"""Independent scheduler around one logical reconciliation oracle per namespace."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from drop_copy_model import COUNTERS, WORD32, Event, Inputs, Key, Lane, Oracle, Report, Status


BAD_NAMESPACE = 13
CRITICAL = frozenset({
    Status.PAIR_CONFLICT, Status.DUP_CONFLICT, Status.MISSING_COPY,
    Status.MISSING_PRIMARY, Status.FULL,
})
PORTS = (
    "primary_ready", "copy_ready", "epoch_ready", "valid", "namespace",
    "status", "source", "key", "expected", "observed", "diff", "age",
    "report_flushed", "epochs", "pending", "metrics", "incidents", "bad_namespaces",
)


@dataclass(frozen=True)
class Observation:
    namespace: int
    event: Event

    def encode(self) -> tuple[int, ...]:
        key, payload = self.event
        return (1, self.namespace, key.packed(), payload.packed())


@dataclass(frozen=True)
class Restart:
    namespace: int
    epoch: int


@dataclass(frozen=True)
class Input:
    primary: Observation | None = None
    copy: Observation | None = None
    restart: Restart | None = None
    ready: bool = True
    reset: bool = False

    def row(self) -> tuple[int, ...]:
        primary = self.primary.encode() if self.primary else (0,)*4
        copy = self.copy.encode() if self.copy else (0,)*4
        control = ((1, self.restart.namespace, self.restart.epoch) if self.restart
                   else (0, 0, 0))
        return (self.reset, self.ready, *primary, *copy, *control)


def pack_low_first(values, width: int) -> int:
    """Place the first logical namespace or counter at the least significant bits."""
    return sum(value << (index * width) for index, value in enumerate(values))


class PartitionModel:
    def __init__(self, count: int, sets: int, wait: int = 24, history: int = 40):
        if count <= 0:
            raise ValueError("namespace count must be positive")
        self.count = count
        self.sets = sets
        self.ledgers = [Oracle(sets, wait, history) for _ in range(count)]
        self.clear_merge()

    def clear_merge(self):
        self.order = deque(range(self.count + 1))
        self.held = None
        self.invalid_report: tuple[int, Report] | None = None
        self.bad_count = 0
        self.last_bad_lane = Lane.COPY
        self.incidents = [False] * self.count

    def _reports(self) -> dict[int, tuple[int, Report]]:
        present = {number: (number, ledger.report)
                   for number, ledger in enumerate(self.ledgers) if ledger.report is not None}
        if self.invalid_report:
            present[self.count] = self.invalid_report
        return present

    def _bad_input(self, request: Input, result_room: bool) -> Lane | None:
        if not result_room:
            return None
        if request.restart and request.restart.namespace >= self.count:
            return Lane.EPOCH
        contenders = [lane for lane, data in ((Lane.PRIMARY, request.primary), (Lane.COPY, request.copy))
                      if data and data.namespace >= self.count]
        if len(contenders) == 2:
            return Lane.PRIMARY if self.last_bad_lane == Lane.COPY else Lane.COPY
        return next(iter(contenders), None)

    def _build_error(self, request: Input, lane: Lane) -> tuple[int, Report]:
        if lane == Lane.EPOCH:
            payload = request.restart
            return (payload.namespace, Report(BAD_NAMESPACE, lane, Key(payload.epoch, 0, 0, 0)))
        payload = request.primary if lane == Lane.PRIMARY else request.copy
        key, observed = payload.event
        self.last_bad_lane = lane
        return (payload.namespace, Report(BAD_NAMESPACE, lane, key, observed=observed))

    def _metrics(self) -> tuple[int, ...]:
        epochs = pack_low_first((ledger.epoch for ledger in self.ledgers), 16)
        pending = pack_low_first((ledger.pending() for ledger in self.ledgers),
                                 (2 * self.sets).bit_length())
        metrics = pack_low_first(
            (pack_low_first((*(ledger.counts[k] for k in COUNTERS), ledger.tick), 32)
             for ledger in self.ledgers), 320,
        )
        return epochs, pending, metrics

    def advance(self, request: Input) -> tuple[int, ...]:
        """Observe the current pins and advance independent ledger clock edges."""
        prior = self._reports()
        selected = self.held if self.held is not None else next(
            (endpoint for endpoint in self.order if endpoint in prior), None,
        )
        published = selected if not request.reset else None
        consume = published if request.ready else None
        bad_room = not request.reset and (self.count not in prior or consume == self.count)
        invalid_lane = self._bad_input(request, bad_room)

        lane_ready = [invalid_lane == lane for lane in (Lane.PRIMARY, Lane.COPY, Lane.EPOCH)]
        # Absent input sidebands are canonical namespace zero in the driver.
        addresses = (
            request.primary.namespace if request.primary else 0,
            request.copy.namespace if request.copy else 0,
            request.restart.namespace if request.restart else 0,
        )
        epoch_word, pending_word, metric_word = self._metrics()
        current_incidents = 0
        for namespace, ledger in enumerate(self.ledgers):
            active_report = prior.get(namespace)
            critical = active_report is not None and active_report[1].status in CRITICAL
            if not request.reset and (critical or self.incidents[namespace]):
                current_incidents |= 1 << namespace
            control = request.restart.epoch if request.restart and addresses[2] == namespace else None
            child_input = Inputs(
                primary=request.primary.event if request.primary and addresses[0] == namespace else None,
                copy=request.copy.event if request.copy and addresses[1] == namespace else None,
                epoch=control,
                ready=consume == namespace,
                reset=request.reset,
            )
            will_restart = control is not None and control > ledger.epoch
            child_pins = ledger.advance(child_input)
            for port, address in enumerate(addresses):
                if address == namespace:
                    lane_ready[port] = child_pins[port]
            if will_restart and child_pins[2]:
                self.incidents[namespace] = False
            elif critical:
                self.incidents[namespace] = True

        output = (0,) * 10
        if published is not None:
            namespace, report = prior[published]
            valid, *body = report.row()
            output = (valid, namespace, *body)

        pins = (*lane_ready, *output, epoch_word, pending_word, metric_word,
                current_incidents, self.bad_count)
        if request.reset:
            self.clear_merge()
        else:
            if consume is not None:
                while self.order[0] != consume:
                    self.order.rotate(-1)
                self.order.rotate(-1)
                self.held = None
            elif published is not None:
                self.held = published
            if bad_room:
                self.invalid_report = None
                if invalid_lane is not None:
                    self.invalid_report = self._build_error(request, invalid_lane)
                    self.bad_count = min(WORD32, self.bad_count + 1)
        return pins
