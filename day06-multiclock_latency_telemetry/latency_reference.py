"""Independent integer model for delay correlation, histograms, and trigger traces."""

from dataclasses import dataclass, field
from enum import IntEnum

LIMIT64 = (1 << 64) - 1
HALF64 = 1 << 63
LIMIT32 = (1 << 32) - 1


class Status(IntEnum):
    STORED = 0
    MATCH = 1
    DUPLICATE = 2
    SET_FULL = 3
    NEGATIVE = 4
    EXPIRED = 5
    DISARMED = 6
    BAD_TIME = 7


def modular_delta(later, earlier):
    return (later - earlier) & LIMIT64


def saturated_add(a, b, maximum):
    value = a + b
    return min(value, maximum), value > maximum


def fit(value, width, description):
    if not 0 <= value < 1 << width:
        raise ValueError(f"{description} {value} cannot fit in {width} bits")
    return value


def fields_to_int(entries):
    result = 0
    for value, width in entries:
        result = (result << width) | fit(value, width, "field")
    return result


@dataclass(frozen=True)
class Geometry:
    tag_width: int = 12
    class_width: int = 2
    probe_width: int = 2
    index_width: int = 3
    scan_log: int = 3

    @property
    def sets(self):
        return 1 << self.index_width

    @property
    def slots(self):
        return 2 * self.sets

    @property
    def sweep_period(self):
        return 1 << self.scan_log

    @property
    def key_width(self):
        return self.tag_width + self.class_width

    def key_bits(self, event):
        return fields_to_int(((event.event_class, self.class_width), (event.tag, self.tag_width)))

    def set_of(self, event):
        key = self.key_bits(event)
        mask = self.sets - 1
        hashed = 0
        while key:
            hashed ^= key & mask
            key >>= self.index_width
        return hashed

    def encode(self, event):
        return fields_to_int((
            (int(event.is_end), 1),
            (event.event_class, self.class_width),
            (event.tag, self.tag_width),
            (event.probe, self.probe_width),
            (event.tick, 64),
        ))


@dataclass(frozen=True)
class Observation:
    tick: int
    tag: int
    event_class: int
    probe: int
    is_end: bool

    @property
    def key(self):
        return self.event_class, self.tag


@dataclass(frozen=True)
class Report:
    status: Status
    tag: int
    event_class: int
    start_probe: int
    end_probe: int
    start_tick: int
    end_tick: int
    duration: int
    observed_tick: int

    @classmethod
    def single(cls, status, event, observed_tick):
        start = None if event.is_end else event
        end = event if event.is_end else None
        return cls(
            status,
            event.tag,
            event.event_class,
            start.probe if start else 0,
            end.probe if end else 0,
            start.tick if start else 0,
            end.tick if end else 0,
            0,
            observed_tick,
        )

    @classmethod
    def pair(cls, first, second, observed_tick):
        if first.key != second.key or first.is_end == second.is_end:
            raise ValueError("pair requires opposite endpoints with the same key")
        start, stop = (second, first) if first.is_end else (first, second)
        delta = modular_delta(stop.tick, start.tick)
        can_measure = delta < HALF64
        return cls(
            Status.MATCH if can_measure else Status.NEGATIVE,
            start.tag,
            start.event_class,
            start.probe,
            stop.probe,
            start.tick,
            stop.tick,
            delta if can_measure else 0,
            observed_tick,
        )

    def unit_word(self, geometry):
        return fields_to_int((
            (int(self.status), 3),
            (self.event_class, geometry.class_width),
            (self.tag, geometry.tag_width),
            (self.start_probe, geometry.probe_width),
            (self.end_probe, geometry.probe_width),
            (self.start_tick, 64),
            (self.end_tick, 64),
            (self.duration, 64),
            (self.observed_tick, 64),
        ))

    def transport_word(self):
        return fields_to_int((
            (int(self.status), 3),
            (self.event_class, 8),
            (self.tag, 16),
            (self.start_probe, 8),
            (self.end_probe, 8),
            (0, 21),
            (self.start_tick, 64),
            (self.end_tick, 64),
            (self.duration, 64),
            (self.observed_tick, 64),
        ))


@dataclass(frozen=True)
class Resident:
    observation: Observation
    admitted: int

    def expired(self, now, limit):
        elapsed = modular_delta(now, self.admitted)
        return limit <= elapsed < HALF64


@dataclass(frozen=True)
class Cycle:
    ready: bool
    visible: object
    pending: int
    evicted: int


class Correlator:
    def __init__(self, geometry):
        self.geometry = geometry
        self.reset()

    def reset(self):
        self.table = {}
        self.report = None
        self.cursor = 0
        self.advance = 0
        self.evictions = 0

    def _apply(self, event, now, limit, armed):
        if not armed:
            return Report.single(Status.DISARMED, event, now)
        if modular_delta(now, event.tick) >= HALF64:
            return Report.single(Status.BAD_TIME, event, now)
        bucket = self.geometry.set_of(event)
        residents = [
            self.table.get(2 * bucket + slot_in_set) for slot_in_set in range(2)
        ]
        candidate = None
        for way, resident in enumerate(residents):
            location = 2 * bucket + way
            if resident is None or resident.expired(now, limit):
                if candidate is None:
                    candidate = location
                continue
            previous = resident.observation
            if previous.key != event.key:
                continue
            if previous.is_end == event.is_end:
                return Report.single(Status.DUPLICATE, event, now)
            del self.table[location]
            return Report.pair(previous, event, now)
        if candidate is None:
            return Report.single(Status.SET_FULL, event, now)
        if candidate in self.table:
            self.evictions = min(self.evictions + 1, LIMIT32)
        self.table[candidate] = Resident(event, now)
        return Report.single(Status.STORED, event, now)

    def _scan(self, now, limit):
        examined = self.cursor
        self.cursor = (self.cursor + 1) % self.geometry.slots
        resident = self.table.get(examined)
        if resident is None or not resident.expired(now, limit):
            return None
        del self.table[examined]
        return Report.single(Status.EXPIRED, resident.observation, now)

    def step(self, *, tick, timeout, observation, sink_ready, armed, reset=False, flush=False):
        if reset:
            self.reset()
            return Cycle(False, None, 0, 0)
        space = self.report is None or sink_ready
        scan = self.advance + 1 == self.geometry.sweep_period
        result = Cycle(space and not (scan or flush), self.report, len(self.table), self.evictions)
        if flush:
            self.reset()
        elif space:
            effective_limit = timeout if 0 < timeout < HALF64 else 1
            self.report = None
            if scan:
                self.report = self._scan(tick, effective_limit)
                self.advance = 0
            else:
                if observation is not None:
                    self.report = self._apply(observation, tick, effective_limit, armed)
                self.advance += 1
        return result


def bucket_of(ticks, bucket_count):
    if ticks < 0:
        raise ValueError("a histogram delay must be nonnegative")
    if bucket_count < 5:
        raise ValueError("histogram geometry must have at least five buckets")
    index = ticks if ticks < 4 else ticks.bit_length() + 1
    return min(index, bucket_count - 1)


def bucket_bounds(index, bucket_count):
    if not 0 <= index < bucket_count:
        raise ValueError("bucket outside histogram geometry")
    if index < 4:
        return index, index
    low = 1 << (index - 2)
    if index + 1 == bucket_count:
        return low, LIMIT64
    return low, 2 * low - 1


@dataclass
class ClassHistogram:
    bucket_count: int
    counter_width: int = 32
    buckets: list = field(init=False)
    minimum: int = 0
    maximum: int = 0
    count: int = 0
    total: int = 0
    overflow: bool = False

    def __post_init__(self):
        self.buckets = [0 for _ in range(self.bucket_count)]

    def add(self, duration):
        fit(duration, 64, "duration")
        which = bucket_of(duration, self.bucket_count)
        maximum_count = (1 << self.counter_width) - 1
        self.buckets[which], bucket_overflow = saturated_add(self.buckets[which], 1, maximum_count)
        if self.count == 0:
            self.minimum = duration
        else:
            self.minimum = min(self.minimum, duration)
        self.maximum = max(self.maximum, duration)
        self.count, count_overflow = saturated_add(self.count, 1, maximum_count)
        self.total, sum_overflow = saturated_add(self.total, duration, LIMIT64)
        self.overflow |= bucket_overflow or count_overflow or sum_overflow

    def snapshot_words(self, number):
        tag = (
            (number, 8),
            (self.bucket_count, 8),
            (self.counter_width, 8),
            (int(self.overflow), 1),
            (0, 3),
        )
        yield fields_to_int(((1, 4), *tag, (self.count, 32), (self.total, 64)))
        yield fields_to_int(((2, 4), (number, 8), (self.maximum >> 12, 52), (self.minimum, 64)))
        for index, count in enumerate(self.buckets):
            maximum_tail = (self.maximum & 0xfff) if index == 0 else 0
            yield fields_to_int((
                (3, 4), (number, 8), (index, 8), (maximum_tail, 12), (0, 64), (count, 32)
            ))


def histogram_words(class_samples, bucket_count, frame_id, tick, counter_width=32):
    yield fields_to_int(((0x4C545331, 32), (frame_id, 32), (tick, 64)))
    for number, samples in enumerate(class_samples):
        metrics = ClassHistogram(bucket_count, counter_width)
        for sample in samples:
            metrics.add(sample)
        yield from metrics.snapshot_words(number)


@dataclass(frozen=True)
class CapturedTrace:
    frame_id: int
    reports: tuple
    trigger_offset: int
    threshold_trigger: bool

    @property
    def trigger(self):
        return self.reports[self.trigger_offset]

    def serialize(self):
        trigger = self.trigger
        yield fields_to_int((
            (0x4C545231, 32),
            (self.frame_id, 32),
            (trigger.observed_tick, 64),
            (len(self.reports), 8),
            (self.trigger_offset, 8),
            (int(trigger.status), 3),
            (int(self.threshold_trigger), 1),
            (0, 12),
        ))
        for report in self.reports:
            yield report.transport_word()


def bounded_trace(reports, depth, post, mask, threshold, frame_id=0):
    if not (1 <= depth <= 128 and 0 <= post < depth):
        raise ValueError("trace depth or post count out of range")
    for index, report in enumerate(reports):
        high = threshold > 0 and report.status == Status.MATCH and report.duration >= threshold
        if not (mask >> report.status) & 1 and not high:
            continue
        last = index + post
        if last >= len(reports):
            return None
        first = max(0, last - depth + 1)
        return CapturedTrace(frame_id, tuple(reports[first:last + 1]), index - first, high)
    return None
