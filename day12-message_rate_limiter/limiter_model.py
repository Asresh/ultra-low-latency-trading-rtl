"""Dictionary-based oracle for atomic token-bucket requests and response timing."""

from collections import Counter
from dataclasses import dataclass, replace

from limiter_protocol import Command, Observation, Operation, Response, Status


@dataclass(frozen=True)
class Statistics:
    """Track successful admissions, limited admissions, and admitted cost."""

    passed: int = 0
    limited: int = 0
    charged: int = 0
    saturated: int = 0

    def add(self, bound, *, passed=0, limited=0, charged=0):
        """Clip each counter separately and retain a sticky lost-count mask."""
        values = [
            self.passed + passed,
            self.limited + limited,
            self.charged + charged,
        ]
        flags = self.saturated
        for bit, value in enumerate(values):
            if value > bound:
                flags |= 1 << bit
        return Statistics(*(min(value, bound) for value in values), saturated=flags)


@dataclass(frozen=True)
class Bucket:
    """Last committed time, policy, token balance, and reporting counters."""

    enabled: bool = False
    anchor: int = 0
    capacity: int = 0
    rate: int = 0
    balance: int = 0
    statistics: Statistics = Statistics()

    @classmethod
    def configure(cls, command):
        """Create a fresh full bucket without inheriting any old statistics."""
        return cls(
            enabled=bool(command.enable),
            anchor=command.tick,
            capacity=command.amount,
            rate=command.rate,
            balance=command.amount,
        )

    def advance(self, geometry, tick):
        """Refill with unbounded integer arithmetic before applying capacity."""
        if not self.enabled:
            return self
        elapsed = geometry.delta(tick, self.anchor)
        if elapsed is None:
            return None
        replenished = min(self.capacity, self.balance + elapsed * self.rate)
        return replace(self, balance=replenished, anchor=tick)

    def response(self, command, status, before):
        """Build the exact expected committed snapshot echoed to the caller."""
        return Response(
            command.op,
            command.key,
            command.tag,
            status,
            int(self.enabled),
            self.anchor,
            self.capacity,
            self.rate,
            before,
            self.balance,
            self.statistics.passed,
            self.statistics.limited,
            self.statistics.charged,
            self.statistics.saturated,
        )


def legal_fields(command):
    """Describe meaningful fields for each operation independently of the RTL."""
    if command.op == Operation.CONFIG:
        if command.enable:
            return 0 < command.amount and 0 <= command.rate <= command.amount
        return command.amount == command.rate == 0
    if command.enable or command.rate:
        return False
    if command.op == Operation.OFFER:
        return command.amount > 0
    return command.amount == 0


class Limiter:
    """Apply one legal change at a time to a dictionary of committed rows."""

    def __init__(self, geometry):
        """Start in reset with no configured bucket and no previous counters."""
        self.geometry = geometry
        self.rows = {}
        self.status_counts = Counter()

    def reset(self):
        """Clear all committed rows and lifetime oracle diagnostics."""
        self.rows.clear()
        self.status_counts.clear()

    def bucket(self, key):
        """Return the current row, treating untouched keys as disabled."""
        return self.rows.get(key, Bucket())

    def decide(self, command):
        """Return the accepted request's decision and change only its own row."""
        if not isinstance(command, Command):
            raise TypeError("expected a command")
        command.validate(self.geometry)
        key = command.key
        previous = self.bucket(key)
        current = previous
        before = previous.balance
        status = Status.OK

        if key >= self.geometry.keys:
            status = Status.BAD_KEY
        elif not legal_fields(command):
            status = Status.BAD_FIELDS
        elif command.op == Operation.CONFIG:
            current = Bucket.configure(command)
        elif command.op == Operation.OFFER and not previous.enabled:
            status = Status.DISABLED
        else:
            advanced = previous.advance(self.geometry, command.tick)
            if advanced is None:
                status = Status.BAD_TICK
            else:
                before = advanced.balance
                current = advanced
                limit = self.geometry.count_max
                if command.op == Operation.CLEAR:
                    current = replace(advanced, statistics=Statistics())
                elif command.op == Operation.OFFER:
                    if advanced.balance < command.amount:
                        status = Status.LIMITED
                        current = replace(
                            advanced, statistics=advanced.statistics.add(limit, limited=1)
                        )
                    else:
                        status = Status.PASS
                        current = replace(
                            advanced,
                            balance=advanced.balance - command.amount,
                            statistics=advanced.statistics.add(
                                limit, passed=1, charged=command.amount
                            ),
                        )
        if key < self.geometry.keys and current != previous:
            self.rows[key] = current
        self.status_counts[status] += 1
        return current.response(command, status, before)


class CycleModel:
    """Add the one-entry ready/valid response stage around the policy oracle."""

    def __init__(self, geometry):
        """Start with a reset bucket table and empty registered response slot."""
        self.policy = Limiter(geometry)
        self.outbox = None

    @property
    def geometry(self):
        """Expose geometry to vector builders and input validators."""
        return self.policy.geometry

    def observe(self, drive):
        """Sample pre-edge state without changing the response or a row."""
        input_ready = int(not drive.reset and (self.outbox is None or drive.ready))
        return Observation(input_ready, int(self.outbox is not None), self.outbox)

    def step(self, drive):
        """Sample signals and then apply reset or the active edge atomically."""
        drive.validate(self.geometry)
        observation = self.observe(drive)
        if drive.reset:
            self.policy.reset()
            self.outbox = None
        elif observation.input_ready:
            self.outbox = self.policy.decide(drive.command) if drive.valid else None
        return observation


class ReferenceVerifier:
    """Check common decision invariants independently of the expected snapshot."""

    def __init__(self, geometry):
        """Keep only configuration limits and accepted request identities."""
        self.geometry = geometry
        self.responses = 0
        self.codes = Counter()

    def observe_decision(self, command, response):
        """Check echoes, cost conservation and saturation shape for one response."""
        if (command.op, command.key, command.tag) != (response.op, response.key, response.tag):
            raise AssertionError("response changed command identity")
        if response.capacity < response.balance:
            raise AssertionError("balance exceeded capacity")
        if response.rate > response.capacity:
            raise AssertionError("refill exceeds capacity")
        if not response.enabled and (
            response.capacity or response.rate or response.balance
            or response.charged or response.passed or response.limited
        ):
            raise AssertionError("disabled bucket retained live state")
        if response.status == Status.PASS:
            if command.op != Operation.OFFER or command.amount <= 0:
                raise AssertionError("non-offer spent tokens")
            if response.before - response.balance != command.amount:
                raise AssertionError("admission was charged the wrong amount")
        if response.status == Status.LIMITED:
            if not 0 <= response.before < command.amount:
                raise AssertionError("limited an offer with enough credit")
            if response.before != response.balance:
                raise AssertionError("limited offer changed credits")
        for bit, counter in enumerate((response.passed, response.limited, response.charged)):
            if response.saturated & (1 << bit) and counter != self.geometry.count_max:
                raise AssertionError(f"saturation bit {bit} was set below the maximum")
        self.responses += 1
        self.codes[response.status] += 1
