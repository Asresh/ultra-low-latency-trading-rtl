"""Cycle specification for configurable mandatory and optional multicast copies."""

from dataclasses import dataclass, replace
from fanout_model import Beat, Fanout, Inputs, OutputBeat, Route, Status, pack


PARTIAL = 7
MAX_COUNTER = (1 << 32)-1


@dataclass(frozen=True)
class Policy:
    members: int = 0
    mandatory: int = 0
    marks: tuple = ()
    may_drop: bool = False

    def valid(self, profile):
        if self.members & ~profile.all_ports or self.mandatory & ~self.members:
            return False
        return all(
            0 <= mark <= profile.depth if (self.members & ~self.mandatory) >> index & 1 else mark == 0
            for index, mark in enumerate(self.marks)
        ) and len(self.marks) == profile.ports


@dataclass(frozen=True)
class PolicyInputs:
    rst: bool = False
    config: tuple = ()
    beat: Beat = None
    ready: int = 0
    report_ready: bool = True

    def words(self, profile):
        group, policy = self.config or (0, Policy())
        beat = self.beat or Beat()
        return (
            self.rst, bool(self.config), group, policy.members, policy.mandatory,
            pack(policy.marks, profile.depth.bit_length()), policy.may_drop,
            self.beat is not None, beat.data, beat.keep, beat.last, beat.group,
            beat.tag, self.ready, self.report_ready,
        )


@dataclass(frozen=True)
class Plan:
    epoch: int
    group: int
    members: int
    mandatory: int
    no_credit: int
    no_slot: bool
    selected: int
    status: int
    can_start: bool


class PolicyFanout:
    def __init__(self, profile, epoch_bits=3):
        self.p = profile
        self.empty = Policy(marks=(0,) * profile.ports)
        self.epoch_modulus = 1 << epoch_bits
        self.core = Fanout(replace(profile, groups=1))
        self.reset()

    def reset(self):
        self.core.reset()
        self.table = [(0, self.empty)] * self.p.groups
        self.stage = "first"
        self.pending = None
        self.plan = None
        self.config_ack = False
        self.config_ok = False
        self.omitted = [0] * self.p.ports

    def admission(self, beat):
        if beat.group >= self.p.groups:
            return Plan(0, beat.group, 0, 0, 0, False, 0, Status.BAD_GROUP, True)
        epoch, policy = self.table[beat.group]
        required, followers = set(), set()
        for port, queue in enumerate(self.core.queues):
            if policy.mandatory >> port & 1:
                if len(queue) < self.p.depth:
                    required.add(port)
            elif policy.members >> port & 1 and len(queue) < policy.marks[port]:
                followers.add(port)
        available = sum(1 << port for port in required|followers)
        no_credit = policy.members & ~available
        no_slot = bool(policy.members and self.core.resident == self.p.slots)
        enough = not policy.mandatory or not (policy.mandatory & no_credit or no_slot)
        selected = available if enough and not no_slot else 0
        if not policy.members:
            status = Status.FILTERED
        elif not selected:
            status = Status.CONGESTED
        elif selected != policy.members:
            status = PARTIAL
        else:
            status = Status.FORWARDED
        return Plan(
            epoch, beat.group, policy.members, policy.mandatory, no_credit, no_slot, selected,
            status, enough or policy.may_drop,
        )

    def step(self, inputs):
        p = self.p
        pause = inputs.rst
        candidate = self.admission(inputs.beat or Beat())
        outgoing = self.pending if self.stage == "inject" else inputs.beat if self.stage == "body" else None
        if outgoing:
            outgoing = replace(outgoing, group=0)
        core_input = Inputs(
            rst=pause, offer=outgoing, ready=inputs.ready,
            report_ready=self.stage == "report" and inputs.report_ready,
        )
        core = self.core.peek(core_input)
        ready = not pause and (
            self.stage == "first" and candidate.can_start or self.stage == "body" and core.s_ready
        )
        receive_first = ready and self.stage == "first" and inputs.beat is not None
        report = core.report if self.stage == "report" and not pause else None
        plan = self.plan if report else None
        status = 0
        enqueued = 0
        if plan:
            status = plan.status
            if status != Status.BAD_GROUP and report.status in (Status.BAD_BEAT, Status.BAD_TAG, Status.TOO_LONG):
                status = report.status
            if status in (Status.FORWARDED, PARTIAL):
                enqueued = plan.selected
        beats = [beat or OutputBeat(0, 0, False, 0) for beat in core.outputs]
        words = (
            not pause, self.config_ack, self.config_ok, ready,
            pack((beat is not None for beat in core.outputs), 1),
            pack((beat.data for beat in beats), p.data_bytes*8),
            pack((beat.keep for beat in beats), p.data_bytes),
            pack((beat.last for beat in beats), 1), pack((beat.tag for beat in beats), p.tag_width),
            report is not None, status, report.tag if report else 0,
            plan.group if plan else 0, plan.epoch if plan else 0,
            plan.members if plan else 0, plan.mandatory if plan else 0,
            plan.no_credit if plan else 0, plan.no_slot if plan else False,
            enqueued, report.selected_bytes if report else 0, pack(core.occupancy, p.depth.bit_length()),
            core.resident, pack(core.delivered, 32), pack(self.omitted, 32),
        )
        if pause:
            self.reset()
            return words

        self.config_ack = bool(inputs.config)
        self.config_ok = False
        if self.config_ack:
            index, new_policy = inputs.config
            if 0 <= index < p.groups and new_policy.valid(p):
                epoch, _ = self.table[index]
                self.table[index] = ((epoch+1) % self.epoch_modulus, new_policy)
                self.config_ok = True

        if receive_first:
            self.pending = inputs.beat
            self.plan = candidate
            core_input = replace(core_input, cfg=(0, Route(candidate.selected)))
            self.stage = "inject"
        elif outgoing and core.s_ready:
            self.stage = "report" if outgoing.last else "body"
        elif report and inputs.report_ready:
            if status in (Status.CONGESTED, PARTIAL):
                optional_omitted = plan.members & ~plan.mandatory & ~enqueued
                for port in range(p.ports):
                    if optional_omitted >> port & 1:
                        self.omitted[port] = min(MAX_COUNTER, self.omitted[port]+1)
            self.stage = "first"
            self.plan = None
            self.pending = None
        self.core.step(core_input)
        return words
