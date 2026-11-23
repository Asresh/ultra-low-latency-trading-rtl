"""Calculate observable book health and timing metrics from a checked trace."""


from __future__ import annotations
from collections import Counter
from dataclasses import asdict, dataclass

from quote_capture import CommandCapture, Flush, QueryCapture, enum_or_number
from quote_schema import Code, Op, State
from quote_verify import latency_summary, percentile


@dataclass(frozen=True)
class StateRun:
    instrument: int
    state: str
    first: int
    last: int
    observations: int
    low_spread: int | None

    @property
    def sampled_span(self):
        """Return distance from first to last observed edge, excluding unsampled claims."""
        return self.last - self.first


class InstrumentStats:
    def __init__(self, instrument, venues):
        """Initialize per-instrument observation, winner, and exposure counters."""
        self.instrument = instrument
        self.venues = venues
        self.states = Counter()
        self.codes = Counter()
        self.bid_wins = Counter()
        self.ask_wins = Counter()
        self.active_seen = Counter()
        self.stale_seen = Counter()
        self.cross_venues = Counter()
        self.price_spread = []
        self.latency = []
        self.bid_ages = []
        self.ask_ages = []
        self.open_run = None
        self.runs = []

    def break_run(self):
        """Close the current observation segment across errors or reset boundaries."""
        if self.open_run is not None:
            self.runs.append(self.open_run)
            self.open_run = None

    def _sample_run(self, state, cycle, spread):
        """Record consecutive sampled evidence for one named market state."""
        previous = self.open_run
        if previous is None or previous.state != state:
            self.break_run()
            self.open_run = StateRun(self.instrument, state, cycle, cycle, 1, spread)
            return
        observed_minimum = previous.low_spread
        if spread is not None:
            if observed_minimum is None or spread < observed_minimum:
                observed_minimum = spread
        self.open_run = StateRun(
            self.instrument, state, previous.first, cycle,
            previous.observations + 1, observed_minimum,
        )

    def _sample_masks(self, result):
        """Tallly active and stale observations independently for each physical venue."""
        for venue in range(self.venues):
            if result.active & (1 << venue):
                self.active_seen[venue] += 1
            if result.stale & (1 << venue):
                self.stale_seen[venue] += 1

    def _sample_winners(self, result):
        """Record independent side winners, winner ages, and two-sided spreads."""
        if result.bid.valid:
            self.bid_wins[result.bid.venue] += 1
            self.bid_ages.append(result.bid.age)
        if result.ask.valid:
            self.ask_wins[result.ask.venue] += 1
            self.ask_ages.append(result.ask.age)
        if result.bid.valid and result.ask.valid:
            self.price_spread.append(result.spread)
            if result.state == State.CROSSED:
                self.cross_venues[f"{result.bid.venue}:{result.ask.venue}"] += 1
            return result.spread
        return None

    def record(self, query):
        """Update successful-query market metrics and reject-code observations."""
        result = query.result
        self.codes[enum_or_number(Code, result.code)] += 1
        self.latency.append(query.latency)
        if result.code != Code.OK:
            self.break_run()
            return
        state = enum_or_number(State, result.state)
        self.states[state] += 1
        self._sample_masks(result)
        spread = self._sample_winners(result)
        self._sample_run(state, query.accepted, spread)

    def _venues(self):
        """Return one complete venue row including zero-observation cases."""
        return [
            {
                "venue": venue,
                "active_observations": self.active_seen[venue],
                "stale_observations": self.stale_seen[venue],
                "bid_wins": self.bid_wins[venue],
                "ask_wins": self.ask_wins[venue],
            }
            for venue in range(self.venues)
        ]

    def summary(self):
        """Build a serializable report without mutating the sampled observations."""
        intervals = self.runs + ([self.open_run] if self.open_run is not None else [])
        return {
            "instrument": self.instrument,
            "query_codes": dict(self.codes),
            "states": dict(self.states),
            "query_latency": latency_summary(self.latency),
            "venues": self._venues(),
            "crossed_bid_ask_venue_pairs": dict(self.cross_venues),
            "bid_ages": latency_summary(self.bid_ages),
            "ask_ages": latency_summary(self.ask_ages),
            "spreads": latency_summary(self.price_spread),
            "state_runs": [asdict(run) | {"sampled_span": run.sampled_span} for run in intervals],
        }


class MarketAnalyzer:
    def __init__(self, profile):
        """Collect portfolio-wide command outcomes and one report per queried instrument."""
        self.profile = profile
        self.instruments = {}
        self.invalid_instruments = Counter()
        self.command_latency = []
        self.command_codes = Counter()
        self.by_operation = Counter()
        self.flushed = Counter()
        self.last_delivery = -1

    def _sequence(self, delivered):
        """Require chronological output while allowing both channels on the same edge."""
        if delivered < self.last_delivery:
            raise ValueError("capture is not ordered by delivery edge")
        self.last_delivery = delivered

    def _query(self, captured):
        """Dispatch each query to its instrument, keeping invalid IDs separate."""
        instrument = captured.request.instrument
        if instrument >= self.profile.instruments:
            self.invalid_instruments[instrument] += 1
            return
        if instrument not in self.instruments:
            self.instruments[instrument] = InstrumentStats(instrument, self.profile.venues)
        self.instruments[instrument].record(captured)

    def record(self, captured):
        """Incorporate a delivered command, delivered query, or reset boundary."""
        if isinstance(captured, Flush):
            self._sequence(captured.cycle)
            self.flushed["resets"] += 1
            self.flushed["commands"] += len(captured.commands)
            self.flushed["queries"] += len(captured.queries)
            for instrument in self.instruments.values():
                instrument.break_run()
            return
        if isinstance(captured, CommandCapture):
            self._sequence(captured.delivered)
            name = enum_or_number(Op, captured.request.op)
            code = enum_or_number(Code, captured.ack.code)
            self.command_codes[code] += 1
            self.by_operation[f"{name}:{code}"] += 1
            self.command_latency.append(captured.latency)
        elif isinstance(captured, QueryCapture):
            self._sequence(captured.delivered)
            self._query(captured)
        else:
            raise ValueError(f"unknown capture detail {captured!r}")

    def summarize(self):
        """Return stable port-level totals and independent per-instrument observations."""
        return {
            "command_latency": latency_summary(self.command_latency),
            "command_codes": dict(self.command_codes),
            "command_outcomes_by_op": dict(self.by_operation),
            "flushes": dict(self.flushed),
            "invalid_instrument_queries": dict(self.invalid_instruments),
            "instruments": [
                self.instruments[instrument].summary() for instrument in sorted(self.instruments)
            ],
        }


def analyze(captured, profile):
    """Aggregate an iterable of typed capture records into a market-health report."""
    metrics = MarketAnalyzer(profile)
    for record in captured:
        metrics.record(record)
    return metrics.summarize()


def render_markdown(report):
    """Summarize sampled outcomes in a simple Markdown report for a replay user."""
    lines = [
        "# Best quote capture report",
        "",
        "Durations describe observed handshakes in clock cycles. Market states are sampled.",
        "",
        f"Commands delivered: {report['command_latency']['count']}",
        "",
        "| Instrument | Observations | Crossed | Locked | Empty | Spread p50 | Query latency p95 |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for book in report["instruments"]:
        states = book["states"]
        headline = (
            book["instrument"], sum(states.values()), states.get("CROSSED", 0),
            states.get("LOCKED", 0), states.get("EMPTY", 0), book["spreads"]["p50"],
            book["query_latency"]["p95"],
        )
        lines.append("| " + " | ".join("—" if item is None else str(item) for item in headline) + " |")
    if not report["instruments"]:
        lines.extend(("", "The capture contains no successful instrument observations."))
    for book in report["instruments"]:
        lines.extend((
            "", f"## Instrument {book['instrument']}", "",
            "| Venue | Active samples | Stale samples | Best bids | Best asks |",
            "| ---: | ---: | ---: | ---: | ---: |",
        ))
        for row in book["venues"]:
            lines.append(
                f"| {row['venue']} | {row['active_observations']} | {row['stale_observations']} "
                f"| {row['bid_wins']} | {row['ask_wins']} |"
            )
    return "\n".join(lines) + "\n"


def cumulative_latency(delays, boundaries):
    """Count deliveries at or below increasing latency bounds for performance checks."""
    bounds = tuple(boundaries)
    if any(type(number) is not int or number < 0 for number in bounds):
        raise ValueError("latency boundaries must be nonnegative integers")
    if any(left >= right for left, right in zip(bounds, bounds[1:])):
        raise ValueError("latency boundaries must be strictly increasing")
    measured = sorted(delays)
    if any(delay < 0 for delay in measured):
        raise ValueError("latency cannot be negative")
    before = 0
    result = {}
    for bound in bounds:
        while before < len(measured) and measured[before] <= bound:
            before += 1
        result[bound] = before
    return result


def compare_latency(baseline, candidate, rank=95):
    """Return percentile displacement between two nonempty independent captures."""
    if not baseline or not candidate:
        raise ValueError("both captures require at least one latency")
    return percentile(candidate, rank) - percentile(baseline, rank)
