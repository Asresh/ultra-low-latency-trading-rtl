"""Independent DCA1 record codec, framing and operation-level replay."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass

from drop_copy_model import RESET_TICK, WORD32, Key, Payload, Status
from drop_copy_partition_model import BAD_NAMESPACE, CRITICAL, Input, Observation, PartitionModel, Restart


MAX64 = (1 << 64) - 1


class AuditError(ValueError):
    pass


def pack(fields, widths) -> int:
    values = tuple(fields)
    if len(values) != len(widths):
        raise AuditError("wrong number of packed fields")
    number = 0
    for value, width in zip(values,widths):
        if not isinstance(value,int) or not 0 <= value < 1 << width:
            raise AuditError(f"{value!r} does not fit an unsigned {width}-bit field")
        number = (number << width) | value
    return number


def unpack(number: int, widths: tuple[int,...]) -> tuple[int,...]:
    if not 0 <= number < 1 << sum(widths):
        raise AuditError("nonzero high padding or negative packed field")
    fields = []
    for length in reversed(widths):
        fields.append(number & ((1 << length) - 1))
        number >>= length
    return tuple(reversed(fields))


def decode_key(value: int) -> Key:
    return Key(*unpack(value,(16,8,16,32,2)))


def decode_payload(value: int) -> Payload:
    return Payload(*unpack(value,(32,24,32,1)))


@dataclass(frozen=True)
class Profile:
    namespaces: int = 3
    sets: int = 4
    wait: int = 24
    history: int = 40
    reset_tick: int = RESET_TICK

    def __post_init__(self):
        if any(type(value) is not int for value in
               (self.namespaces,self.sets,self.wait,self.history,self.reset_tick)):
            raise AuditError("replay geometry fields must be integers")
        if not 1 <= self.namespaces <= 32:
            raise AuditError("namespace count outside the host decoder range")
        if not 1 <= self.sets <= 128 or self.sets & (self.sets - 1):
            raise AuditError("set count must be a positive power of two up to 128")
        if not (0 < self.wait <= 0x7FFFFFFF and 0 < self.history <= 0x7FFFFFFF):
            raise AuditError("positive wait and history required")
        if not 0 <= self.reset_tick <= WORD32:
            raise AuditError("reset tick must fit 32 bits")

    @property
    def ns_w(self) -> int:
        return self.namespaces.bit_length()

    @property
    def count_w(self) -> int:
        return (2*self.sets).bit_length()

    @property
    def observation_widths(self) -> tuple[int,...]:
        return self.ns_w,74,89

    @property
    def restart_widths(self) -> tuple[int,...]:
        return self.ns_w,16

    @property
    def report_widths(self) -> tuple[int,...]:
        return self.ns_w,4,2,74,89,89,4,32,self.count_w

    @property
    def marker_widths(self) -> tuple[int,...]:
        return 1,32,32,16*self.namespaces,self.count_w*self.namespaces,self.namespaces,32

    @property
    def record_widths(self) -> tuple[int,...]:
        event_bits = sum(self.observation_widths)
        return (64,64,64,5,event_bits,event_bits,sum(self.restart_widths),
                sum(self.report_widths),sum(self.marker_widths))

    @property
    def record_bits(self) -> int:
        return sum(self.record_widths)


@dataclass(frozen=True)
class Marker:
    terminal: int
    tag: int
    tick: int
    epochs: int
    pending: int
    incidents: int
    bad_count: int

    def states(self, profile: Profile) -> list[dict[str,int]]:
        return [
            {
                "namespace": index,
                "epoch": (self.epochs >> (16*index)) & 0xFFFF,
                "pending": (self.pending >> (profile.count_w*index)) & ((1 << profile.count_w)-1),
                "incident": (self.incidents >> index) & 1,
            }
            for index in range(profile.namespaces)
        ]


@dataclass(frozen=True)
class Record:
    run: int
    index: int
    cycle: int
    primary: Observation | None = None
    copy: Observation | None = None
    restart: Restart | None = None
    report: tuple[int,...] | None = None
    marker: Marker | None = None

    def encode(self, profile: Profile) -> int:
        primary = self.primary.encode()[1:] if self.primary else None
        copy = self.copy.encode()[1:] if self.copy else None
        restart = (self.restart.namespace,self.restart.epoch) if self.restart else None
        marker = tuple(asdict(self.marker).values()) if self.marker else None
        groups = (
            (primary,profile.observation_widths),
            (copy,profile.observation_widths),
            (restart,profile.restart_widths),
            (self.report,profile.report_widths),
            (marker,profile.marker_widths),
        )
        active = sum(1 << index for index,(value,_) in enumerate(groups) if value is not None)
        values = tuple(pack(value,widths) if value is not None else 0 for value,widths in groups)
        return pack((self.run,self.index,self.cycle,active,*values),profile.record_widths)

    @classmethod
    def decode(cls, packed: int, profile: Profile) -> Record:
        run,index,cycle,valid,*payload = unpack(packed,profile.record_widths)
        if not run or not index or not valid:
            raise AuditError("record requires an identity, index, and captured event")
        for bit,value in enumerate(payload):
            if not (valid & (1 << bit)) and value:
                raise AuditError("absent channel contains noncanonical data")
        capture = [bool(valid & (1 << bit)) for bit in range(5)]

        def observation(raw: int) -> Observation:
            namespace,key,body = unpack(raw,profile.observation_widths)
            return Observation(namespace,(decode_key(key),decode_payload(body)))

        primary = observation(payload[0]) if capture[0] else None
        copy = observation(payload[1]) if capture[1] else None
        restart = Restart(*unpack(payload[2],profile.restart_widths)) if capture[2] else None
        report = unpack(payload[3],profile.report_widths) if capture[3] else None
        marker = Marker(*unpack(payload[4],profile.marker_widths)) if capture[4] else None
        if report and report[1] > BAD_NAMESPACE:
            raise AuditError("reserved diagnostic status")
        if marker and marker.terminal and valid != 0b10000:
            raise AuditError("terminal record also contains accepted reconciliation work")
        return cls(run,index,cycle,primary,copy,restart,report,marker)


def describe_status(code: int) -> str:
    return "BAD_NAMESPACE" if code == BAD_NAMESPACE else Status(code).name


def expand_report(raw: tuple[int,...]) -> dict:
    namespace,status,lane,key,expected,observed,diff,age,flushed = raw
    return {
        "namespace": namespace,
        "status": describe_status(status),
        "lane": lane,
        "key": asdict(decode_key(key)),
        "expected": asdict(decode_payload(expected)),
        "observed": asdict(decode_payload(observed)),
        "diff": diff,
        "age": age,
        "flushed": flushed,
    }


class Replay:
    """Reconstruct the complete partition machine from accepted-event samples."""

    def __init__(self, profile: Profile, max_cycles: int = 1_000_000_000,
                 namespace: int | None = None, execution: int | None = None):
        if not 0 < max_cycles <= MAX64:
            raise AuditError("invalid replay budget")
        self.profile = profile
        self.machine = PartitionModel(profile.namespaces,profile.sets,profile.wait,profile.history,
                                      profile.reset_tick)
        self.max_cycles = max_cycles
        self.namespace = namespace
        self.execution = execution
        self.cycle = 0
        self.index = 0
        self.run: int | None = None
        self.terminal: Record | None = None
        self.markers = []
        self.reports = Counter()
        self.trace = []
        self.accepted = Counter()

    def skip_idle(self, target: int) -> None:
        """Fast-forward quiet clocks without skipping an autonomous timeout."""
        while self.cycle < target:
            self.machine.advance(Input(ready=False))
            self.cycle += 1
            rows = self.machine.ledgers
            if self.machine.held is None and (self.machine.invalid_report or any(row.report for row in rows)):
                # A new raw diagnostic needs its first arbitration edge.
                continue
            if any(row.report and row.report.status in CRITICAL and not self.machine.incidents[index]
                   for index,row in enumerate(rows)):
                continue

            future = target - self.cycle
            for row in rows:
                if row.report is None:
                    for entry in row.entries.values():
                        if not entry.closed:
                            future = min(future,max(0,row.wait-row.age(entry)))
            if future:
                for row in rows:
                    # Retire closed records at an edge inside the omitted
                    # interval even when the final 32-bit tick has wrapped.
                    retired = [key for key,entry in row.entries.items()
                               if entry.closed and row.age(entry)+future-1 >= row.history]
                    for key in retired:
                        del row.entries[key]
                    row.tick = (row.tick + future) & WORD32
                self.cycle += future

    def advance(self, record: Record) -> None:
        if self.terminal:
            raise AuditError("record follows the terminal marker")
        if self.run is None:
            self.run = record.run
        if record.run != self.run or record.index != self.index + 1:
            raise AuditError("run identity changed or record indexes are not contiguous")
        if record.cycle < self.cycle:
            raise AuditError(f"record {record.index}: cycle repeated or regressed")
        if record.cycle >= self.max_cycles:
            raise AuditError("record exceeds configured cycle replay budget")
        self.skip_idle(record.cycle)

        offered = Input(record.primary,record.copy,record.restart,bool(record.report))
        sampled = self.machine.advance(offered)
        for index,request in enumerate((record.primary,record.copy,record.restart)):
            if request and not sampled[index]:
                raise AuditError(f"record {record.index}: unready accepted operation on port {index}")
        if record.report is not None and (not sampled[3] or tuple(sampled[4:13]) != record.report):
            raise AuditError(f"record {record.index}: diagnostic differs from exact reconciliation replay")

        self.index = record.index
        self.cycle += 1
        for name,value in (("primary",record.primary),("copy",record.copy),("restart",record.restart)):
            if value is not None:
                self.accepted[name] += 1
        if record.report:
            status = describe_status(record.report[1])
            self.reports[status] += 1
            if self.execution is not None or self.namespace is not None:
                expanded = expand_report(record.report)
                if ((self.namespace is None or expanded["namespace"] == self.namespace)
                    and (self.execution is None or expanded["key"]["execution"] == self.execution)):
                    self.trace.append({"cycle":record.cycle,"record":record.index,**expanded})

        if record.marker:
            m = record.marker
            tick = (sampled[15] >> (9*32)) & WORD32
            expected = (tick,sampled[13],sampled[14],sampled[16],sampled[17])
            if (m.tick,m.epochs,m.pending,m.incidents,m.bad_count) != expected:
                raise AuditError(f"record {record.index}: marker state differs from partition replay")
            self.markers.append({"record":record.index,"cycle":record.cycle,"tag":m.tag,
                                 "tick":tick,"terminal":bool(m.terminal),
                                 "bad_namespace_count":m.bad_count,"state":m.states(self.profile)})
            if m.terminal:
                self.terminal = record

    def summary(self, require_terminal: bool = True) -> dict:
        if not self.index:
            raise AuditError("empty investigation run")
        if require_terminal and self.terminal is None:
            raise AuditError("run has no terminal marker")
        return {
            "run": self.run,
            "records": self.index,
            "through_cycle": self.cycle - 1,
            "terminal": self.terminal is not None,
            "accepted": dict(sorted(self.accepted.items())),
            "report_counts": dict(sorted(self.reports.items())),
            "markers": self.markers,
            "selected_reports": self.trace,
        }
