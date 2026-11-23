"""Check an actual simulation capture against independent executable rules."""

from collections import Counter, deque
from dataclasses import dataclass, fields
from itertools import zip_longest
import json
from pathlib import Path

from quote_model import Machine
from quote_schema import Code, Drive, Result, Sample, State, Winner, decimal_row, row_text


class TraceError(ValueError):
    """A row or accepted transaction violated its documented trace contract."""


@dataclass(frozen=True)
class ComparedCycle:
    cycle: int
    drive: Drive
    sample: Sample


def write_drives(path, drives, profile):
    """Persist decimal input rows after locally validating every record."""
    with Path(path).open("w", encoding="ascii") as handle:
        for drive in drives:
            drive.check(profile)
            handle.write(row_text(drive.encode()))
            handle.write("\n")


def read_drives(path, profile):
    """Yield decoded input rows and identify bad fields by physical line number."""
    with Path(path).open(encoding="ascii") as handle:
        for number, line in enumerate(handle, 1):
            try:
                yield Drive.decode(decimal_row(line, 18), profile)
            except ValueError as exc:
                raise TraceError(f"{path}:{number}: {exc}") from exc


def read_samples(path, profile):
    """Yield sampled output and require consecutive edge numbers starting at zero."""
    with Path(path).open(encoding="ascii") as handle:
        for wanted, line in enumerate(handle):
            try:
                row = decimal_row(line, 26)
                if row[0] != wanted:
                    raise ValueError(f"expected edge {wanted}, received {row[0]}")
                yield Sample.decode(row[1:], profile)
            except ValueError as exc:
                raise TraceError(f"{path}:{wanted + 1}: {exc}") from exc


def compare_fields(observed, expected, prefix=""):
    """Return typed field differences, expanding candidates without losing context."""
    if observed == expected:
        return []
    if observed is None or expected is None or type(observed) is not type(expected):
        return [f"{prefix.rstrip('.') or 'value'}={observed!r} expected={expected!r}"]
    if not hasattr(observed, "__dataclass_fields__"):
        return [f"{prefix.rstrip('.')}={observed!r} expected={expected!r}"]
    mismatches = []
    for field in fields(expected):
        actual_field = getattr(observed, field.name)
        reference_field = getattr(expected, field.name)
        mismatches.extend(compare_fields(
            actual_field, reference_field, f"{prefix}{field.name}."
        ))
    return mismatches


def payload_defects(result, profile):
    """Cross-check observed fields without relying on the software book or ranking algorithm."""
    issues = []
    if result.active & result.stale:
        issues.append("one venue is both active and stale")
    if result.active & ~profile.venue_mask or result.stale & ~profile.venue_mask:
        issues.append("a mask names a venue outside this profile")
    if result.code != Code.OK:
        if result.code not in (Code.BAD_AGE, Code.BAD_INST):
            issues.append("response has a code reserved for updates")
        if result.active or result.stale:
            issues.append("an invalid query exposed an eligibility mask")
        if result.bid.valid or result.ask.valid:
            issues.append("an invalid query exposed a winner")
    for label, winner in (("bid", result.bid), ("ask", result.ask)):
        if not winner.valid and winner != Winner():
            issues.append(f"absent {label} is not all zero")
        if winner.valid:
            if not (result.active & (1 << winner.venue)):
                issues.append(f"{label} winner is not an active venue")
            if not winner.price or not winner.qty:
                issues.append(f"{label} winner has a zero field")
            if winner.age >= profile.time_half:
                issues.append(f"{label} winner has ambiguous age")
    if result.bid.valid and result.ask.valid:
        delta = result.ask.price - result.bid.price
        if result.spread != delta:
            issues.append("spread does not equal signed ask minus bid")
        if result.state not in (State.OPEN, State.LOCKED, State.CROSSED):
            issues.append("two-sided result has a one-sided state")
        elif (result.state == State.OPEN and delta <= 0 or
              result.state == State.LOCKED and delta != 0 or
              result.state == State.CROSSED and delta >= 0):
            issues.append("state does not match spread sign")
    else:
        if result.spread != 0:
            issues.append("one-sided result published a spread")
        if result.state != (State.BID_ONLY if result.bid.valid else
                            State.ASK_ONLY if result.ask.valid else State.EMPTY):
            issues.append("state does not match which sides are present")
    return issues


def percentile(samples, percent):
    """Return an integer observed percentile using the nearest-rank convention."""
    if not samples:
        return None
    if not 0 <= percent <= 100:
        raise ValueError("percent must be in [0, 100]")
    values = sorted(samples)
    index = max(0, (len(values) * percent + 99) // 100 - 1)
    return values[index]


def latency_summary(samples):
    """Summarize offered-to-delivered timing without discarding unobserved cases."""
    if not samples:
        return {"count": 0, "min": None, "p50": None, "p95": None, "max": None}
    return {
        "count": len(samples), "min": min(samples),
        "p50": percentile(samples, 50), "p95": percentile(samples, 95),
        "max": max(samples),
    }


class Scoreboard:
    def __init__(self, profile, name):
        """Start reference simulation and independent handshake, stall, and timing tracking."""
        self.profile = profile
        self.name = name
        self.machine = Machine(profile)
        self.command_history = deque()
        self.query_history = deque()
        self.command_latencies = []
        self.query_latencies = []
        self.previous = None
        self.counts = Counter()
        self.codes = Counter()
        self.states = Counter()
        self.command_codes = Counter()
        self.query_codes = Counter()
        self.winners = Counter()

    def require(self, condition, cycle, message):
        """Raise a scenario-and-edge diagnostic at the first offending record."""
        if not condition:
            raise TraceError(f"{self.name} cycle {cycle}: {message}")

    def _stable(self, old_valid, old_payload, ready, new_payload, channel, cycle):
        """Require every source to hold an unconsumed payload until the next edge."""
        if old_valid and not ready:
            self.require(old_payload == new_payload, cycle, f"stalled {channel} payload changed")

    def _check_stability(self, drive, sample, cycle):
        """Check all four independent ready/valid contracts across consecutive live cycles."""
        if self.previous is None:
            return
        previous_drive, previous_sample = self.previous
        self._stable(
            previous_drive.command is not None, previous_drive.command,
            previous_sample.cmd_ready, drive.command, "command", cycle,
        )
        self._stable(
            previous_drive.query is not None, previous_drive.query,
            previous_sample.query_ready, drive.query, "query", cycle,
        )
        self._stable(
            previous_sample.ack is not None, previous_sample.ack,
            previous_drive.ack_ready, sample.ack, "acknowledgement", cycle,
        )
        self._stable(
            previous_sample.result is not None, previous_sample.result,
            previous_drive.result_ready, sample.result, "result", cycle,
        )

    def _drain(self, drive, sample, cycle):
        """Check identities of transfers and record timing from independent accepting edges."""
        ack = sample.transferred_ack(drive)
        if ack is not None:
            self.require(bool(self.command_history), cycle, "unexpected acknowledgement")
            sent, accepted_at = self.command_history.popleft()
            self.require(sent.tag == ack.tag, cycle, "out-of-order acknowledgement tag")
            self.command_latencies.append(cycle - accepted_at)
            self.codes[Code(ack.code).name] += 1
            self.command_codes[Code(ack.code).name] += 1
        result = sample.transferred_result(drive)
        if result is not None:
            self.require(bool(self.query_history), cycle, "unexpected result")
            sent, accepted_at, accepted_tick = self.query_history.popleft()
            self.require(sent.tag == result.tag, cycle, "out-of-order result tag")
            self.require(accepted_tick == result.tick, cycle, "result sampled a later clock tick")
            self.query_latencies.append(cycle - accepted_at)
            self.codes[Code(result.code).name] += 1
            self.query_codes[Code(result.code).name] += 1
            self.states[State(result.state).name] += 1
            if result.bid.valid:
                self.winners[f"bid:{result.bid.venue}"] += 1
            if result.ask.valid:
                self.winners[f"ask:{result.ask.venue}"] += 1

    def _accept(self, drive, sample, cycle):
        """Append source handshakes after processing output to preserve pre-state ordering."""
        command_fire = sample.accepted_command(drive)
        query_fire = sample.accepted_query(drive)
        if command_fire:
            self.command_history.append((drive.command, cycle))
            self.counts["commands"] += 1
        if query_fire:
            self.query_history.append((drive.query, cycle, drive.tick))
            self.counts["queries"] += 1
        if command_fire and query_fire:
            self.counts["parallel"] += 1
        if drive.command is not None and not command_fire:
            self.counts["command_stalls"] += 1
        if drive.query is not None and not query_fire:
            self.counts["query_stalls"] += 1
        if sample.ack is not None and not drive.ack_ready:
            self.counts["ack_stalls"] += 1
        if sample.result is not None and not drive.result_ready:
            self.counts["result_stalls"] += 1

    def _reset(self):
        """Discard queues at the reset edge and account for aborted transfers."""
        self.counts["reset_cycles"] += 1
        self.counts["reset_flushed_commands"] += len(self.command_history)
        self.counts["reset_flushed_queries"] += len(self.query_history)
        self.command_history.clear()
        self.query_history.clear()
        self.previous = None

    def check(self, cycle, drive, sample):
        """Compare one visible bus edge and update reference, invariants, and statistics."""
        self.require(self.machine.cycle == cycle, cycle, "nonconsecutive cycles")
        expected = self.machine.step(drive)
        mismatches = compare_fields(sample, expected)
        self.require(not mismatches, cycle, "; ".join(mismatches))
        self.counts["cycles"] += 1
        if drive.reset:
            self._reset()
            return
        self._check_stability(drive, sample, cycle)
        if sample.result is not None:
            issues = payload_defects(sample.result, self.profile)
            self.require(not issues, cycle, "; ".join(issues))
        self._drain(drive, sample, cycle)
        self._accept(drive, sample, cycle)
        self.machine.book.check_invariants()
        self.previous = drive, sample

    def finish(self, workload=None):
        """Reject incomplete or undercovered runs, then report measured handshakes."""
        self.require(bool(self.counts["cycles"]), 0, "empty trace")
        last = self.machine.cycle
        self.require(not self.command_history and not self.query_history, last, "undrained trace")
        if workload is not None:
            missing_codes = {Code(number).name for number in workload.required_codes} - self.codes.keys()
            missing_states = {State(number).name for number in workload.required_states} - self.states.keys()
            self.require(not missing_codes, last, f"missing error code coverage {sorted(missing_codes)}")
            self.require(not missing_states, last, f"missing market state coverage {sorted(missing_states)}")
            self.require(self.counts["command_stalls"] >= workload.minimum_cmd_stalls,
                         last, "insufficient command stalls")
            self.require(self.counts["query_stalls"] >= workload.minimum_query_stalls,
                         last, "insufficient query stalls")
            self.require(self.counts["parallel"] >= workload.minimum_parallel,
                         last, "insufficient simultaneous reads and writes")
            if workload.exact_query_latency is not None:
                self.require(set(self.query_latencies) == {workload.exact_query_latency},
                             last, "unblocked response took unexpected latency")
        return self.summary()

    def summary(self):
        """Make counters and latency observations available as portable JSON data."""
        return {
            "scenario": self.name,
            "edges": dict(self.counts),
            "command_latency": latency_summary(self.command_latencies),
            "query_latency": latency_summary(self.query_latencies),
            "command_codes": dict(self.command_codes),
            "query_codes": dict(self.query_codes),
            "market_states": dict(self.states),
            "winning_venues": dict(self.winners),
        }


def compare_trace(drives, samples, profile, name="capture", workload=None):
    """Check both iterables completely and require identical edge counts."""
    board = Scoreboard(profile, name)
    for cycle, (drive, sample) in enumerate(zip_longest(drives, samples)):
        if drive is None or sample is None:
            raise TraceError(f"{name} cycle {cycle}: drive/trace length mismatch")
        board.check(cycle, drive, sample)
    return board.finish(workload)


def save_report(path, result):
    """Write a stable JSON report that can be compared across runs."""
    Path(path).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
