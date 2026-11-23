"""Reference wire contract for the independent data and audit consumers."""

from dataclasses import dataclass
from typing import Optional, Tuple

from limiter_model import Limiter
from limiter_protocol import Command, Status, ZERO_COMMAND, bool_bit, unsigned


AUDIT_NAMES = (
    "op", "key", "tag", "status", "balance", "passed", "limited", "charged", "saturated",
)


@dataclass(frozen=True)
class GateDrive:
    """Input request and the two separately controlled sink-ready signals."""

    reset: int = 0
    valid: int = 0
    audit_ready: int = 1
    output_ready: int = 1
    data: int = 0
    command: Command = ZERO_COMMAND

    def validate(self, geometry, data_w):
        """Check port fields before calling the independent policy model."""
        for name in ("reset", "valid", "audit_ready", "output_ready"):
            bool_bit(getattr(self, name), name)
        unsigned(self.data, data_w, "data")
        self.command.validate(geometry)

    def words(self):
        """Export one sample as fixed control words followed by the command."""
        return (
            self.reset, self.valid, self.audit_ready, self.output_ready,
            self.data, *self.command.words(),
        )


@dataclass(frozen=True)
class GatePins:
    """Snapshot of three valid/ready interfaces before their active clock edge."""

    input_ready: int
    audit: Optional[Tuple[int, ...]]
    output: Optional[Tuple[int, int, int]]

    def words(self):
        """Preserve ready and valid and suppress payload from inactive interfaces."""
        return (
            self.input_ready,
            int(self.audit is not None),
            int(self.output is not None),
            *(self.audit or (0,) * len(AUDIT_NAMES)),
            *(self.output or (0, 0, 0)),
        )


class GateModel:
    """Store one request result until all required consumers acknowledge it."""

    def __init__(self, geometry, data_w=32):
        """Begin with an empty message slot and a disabled policy table."""
        self.implementation = Limiter(geometry)
        self.data_w = data_w
        self.audit = None
        self.output = None

    def observe(self, drive):
        """Expose each still-pending interface without changing any state."""
        if drive.reset:
            return GatePins(0, None, None)
        has_room = (
            (self.audit is None or drive.audit_ready)
            and (self.output is None or drive.output_ready)
        )
        return GatePins(int(has_room), self.audit, self.output)

    def step(self, drive):
        """Consume any delivered interfaces, then optionally install new work."""
        drive.validate(self.implementation.geometry, self.data_w)
        pins = self.observe(drive)
        if drive.reset:
            self.implementation.reset()
            self.audit = None
            self.output = None
            return pins
        if drive.audit_ready:
            self.audit = None
        if drive.output_ready:
            self.output = None
        if drive.valid and pins.input_ready:
            reply = self.implementation.decide(drive.command)
            self.audit = tuple(int(getattr(reply, name)) for name in AUDIT_NAMES)
            if reply.status == Status.PASS:
                self.output = (reply.key, reply.tag, drive.data)
        return pins
