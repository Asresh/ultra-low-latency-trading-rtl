"""Analyze traffic bursts without simulating limiter state or choosing a policy."""

from dataclasses import dataclass

from limiter_tuning import verify_demands


@dataclass(frozen=True)
class Window:
    """One interval of requests that witnesses a bandwidth or capacity bound."""

    first_index: int
    last_index: int
    first_tick: int
    last_tick: int
    requests: int
    cost: int
    refill: int
    required_capacity: int

    def to_record(self):
        """Produce a self-contained explanation of the selected burst."""
        return {
            "first_index": self.first_index,
            "last_index": self.last_index,
            "first_tick": self.first_tick,
            "last_tick": self.last_tick,
            "requests": self.requests,
            "cost": self.cost,
            "refill": self.refill,
            "required_capacity": self.required_capacity,
        }


def find_capacity(demands, rate):
    """Find the greatest interval demand minus refill in one linear pass."""
    if type(rate) is not int or rate < 0:
        raise ValueError("refill rate must be a nonnegative integer")
    traffic = tuple(demands)
    verify_demands(traffic)
    prefix = 0
    best_anchor = None
    anchor_cost = 0
    anchor_index = 0
    required = None
    for index, item in enumerate(traffic):
        # For any left edge j and right edge i, the cost to cover is
        # P[i+1] - P[j] - rate*(tick[i] - tick[j]). For fixed i,
        # only the largest value of rate*tick[j] - P[j] is relevant.
        anchor = rate * item.tick - prefix
        if best_anchor is None or anchor > best_anchor:
            best_anchor = anchor
            anchor_cost = prefix
            anchor_index = index
        prefix += item.cost
        capacity = prefix - rate * item.tick + best_anchor
        if required is None or capacity > required.required_capacity:
            first_tick = traffic[anchor_index].tick
            refill = rate * (item.tick - first_tick)
            required = Window(
                anchor_index,
                index,
                first_tick,
                item.tick,
                index - anchor_index + 1,
                prefix - anchor_cost,
                refill,
                capacity,
            )
    return required


def max_cost_in_period(demands, duration):
    """Find the most costly half-open tick window of the given duration."""
    if type(duration) is not int or duration <= 0:
        raise ValueError("duration must be a positive integer")
    traffic = tuple(demands)
    verify_demands(traffic)
    start = 0
    amount = 0
    max_amount = 0
    winner = None
    for stop, item in enumerate(traffic):
        amount += item.cost
        while item.tick - traffic[start].tick >= duration:
            amount -= traffic[start].cost
            start += 1
        if amount > max_amount:
            max_amount = amount
            winner = Window(
                start, stop, traffic[start].tick, item.tick, stop - start + 1,
                amount, 0, amount
            )
    return winner


def capacity_grid(demands, rates, *, max_capacity=None):
    """Analyze full-pass requirements before replaying a policy grid."""
    if max_capacity is not None and (
        type(max_capacity) is not int or max_capacity <= 0
    ):
        raise ValueError("max_capacity must be positive")
    traffic = tuple(demands)
    slopes = sorted(set(rates))
    if not slopes:
        raise ValueError("provide at least one rate")
    report = []
    for rate in slopes:
        window = find_capacity(traffic, rate)
        capacity = max(1, rate, window.required_capacity if window else 0)
        feasible = max_capacity is None or capacity <= max_capacity
        report.append({
            "rate": rate,
            "minimum_configured_capacity": capacity,
            "fits_token_width": feasible,
            "witness": window.to_record() if window else None,
        })
    return report


def period_grid(demands, durations):
    """Describe operational burst pressure across explicitly requested scales."""
    traffic = tuple(demands)
    result = []
    for duration in sorted(set(durations)):
        witness = max_cost_in_period(traffic, duration)
        result.append({
            "duration": duration,
            "maximum_cost": witness.cost if witness else 0,
            "witness": witness.to_record() if witness else None,
        })
    return result
