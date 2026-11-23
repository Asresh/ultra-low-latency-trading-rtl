"""Measure decisions reached by an audit, including age and counter boundaries."""

from collections import Counter
from dataclasses import dataclass, field

from limiter_protocol import Operation, Status


@dataclass
class DecisionCoverage:
    """Track category counts from actual, already validated response transfers."""

    geometry: object
    counts: Counter = field(default_factory=Counter)
    latest: dict = field(default_factory=dict)
    previous_key: int = -1

    def reset(self, outstanding):
        """Start a new hardware epoch and remember whether a result was cancelled."""
        self.counts["reset_idle" if not outstanding else "reset_pending"] += 1
        self.latest.clear()
        self.previous_key = -1

    def observe_time(self, command, before, response):
        """Classify legal movement, modular wrap, or a rejected time reversal."""
        if not before.enabled or command.op == Operation.CONFIG:
            return
        delta = (command.tick - before.anchor) & self.geometry.tick_mask
        result = response.status
        if result == Status.BAD_TICK:
            if delta == self.geometry.half:
                self.counts["time_half_range"] += 1
            elif delta > self.geometry.half:
                self.counts["time_backwards"] += 1
            return
        if result not in (Status.OK, Status.PASS, Status.LIMITED):
            return
        if delta == 0:
            self.counts["same_tick"] += 1
        if delta and command.tick < before.anchor:
            self.counts["time_forward_wrap"] += 1
        if delta == self.geometry.half - 1:
            self.counts["time_max_forward"] += 1
        available = before.balance + delta * before.rate
        if available > before.capacity:
            self.counts["refill_clipped"] += 1
        if delta and before.rate == 0:
            self.counts["time_zero_rate"] += 1

    def observe_tokens(self, command, response):
        """Classify decisions by the exact quota pressure seen on the wire."""
        if response.status == Status.PASS:
            if response.balance == 0:
                self.counts["spent_exactly_available"] += 1
            if response.balance > 0:
                self.counts["balance_retained"] += 1
        if response.status == Status.LIMITED:
            if response.before == 0:
                self.counts["limited_empty"] += 1
            if response.before + 1 == command.amount:
                self.counts["limited_by_one"] += 1
            if command.amount > response.capacity:
                self.counts["limited_larger_than_capacity"] += 1

    def observe_statistics(self, before, response):
        """Identify first lost increments and later clearing of sticky flags."""
        history = before.saturated if before else 0
        if history and not response.saturated:
            self.counts["saturation_cleared"] += 1
        for bit, label in enumerate(("pass", "limited", "charged")):
            mask = 1 << bit
            if response.saturated & mask:
                if history & mask:
                    self.counts[f"saturation_kept_{label}"] += 1
                else:
                    self.counts[f"saturation_first_{label}"] += 1

    def transfer(self, command, response):
        """Classify decoded fields and update the last state for this key."""
        self.counts[f"operation_{command.op.name.lower()}"] += 1
        self.counts[f"status_{response.status.name.lower()}"] += 1
        if response.key != self.previous_key and self.previous_key != -1:
            self.counts["key_changed"] += 1
        if response.key >= self.geometry.keys:
            self.counts["unused_key_encoding"] += 1
            return
        before = self.latest.get(response.key)
        if before is not None:
            self.observe_time(command, before, response)
        self.observe_tokens(command, response)
        self.observe_statistics(before, response)
        if command.op == Operation.CONFIG and response.status == Status.OK:
            self.counts["configuration_enabled" if response.enabled else "configuration_disabled"] += 1
        self.latest[response.key] = response
        self.previous_key = response.key

    def export(self):
        """Return a stable mapping suitable for a trace summary."""
        return dict(sorted(self.counts.items()))


def required_for_standard_matrix(names):
    """List categories guaranteed by the standard boundary and saturation plans."""
    required = {
        "reset_pending", "configuration_enabled", "configuration_disabled",
        "time_half_range", "time_backwards", "time_forward_wrap", "time_max_forward",
        "spent_exactly_available", "limited_empty", "limited_by_one", "time_zero_rate",
        "same_tick", "refill_clipped",
    }
    if "small" in names or "wide_tokens" in names or "single" in names or "wide_tick" in names:
        required.update({"saturation_first_pass", "saturation_first_limited", "saturation_first_charged"})
    return required
