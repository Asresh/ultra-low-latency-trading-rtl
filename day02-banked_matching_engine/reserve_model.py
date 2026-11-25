"""Transaction oracle and packed interface for the banked reserve matcher."""

from dataclasses import dataclass, fields, replace
from enum import IntEnum


class Operation(IntEnum):
    NEW = 0
    CANCEL = 1
    REDUCE = 2
    LEVEL = 3
    ORDER = 4
    SNAPSHOT = 5
    IMPORT = 6
    MANAGE = 7


class ImportPart(IntEnum):
    BEGIN = 0
    ROW = 1
    COMMIT = 2
    ABORT = 3


class Management(IntEnum):
    REPLACE = 0
    CANCEL = 1
    PREVIEW = 2


class Scope(IntEnum):
    OWNER = 0
    SIDE = 1
    LEVEL = 2
    THROUGH = 3


class TimeInForce(IntEnum):
    GTC = 0
    IOC = 1
    POST = 2
    FOK = 3


class Prevention(IntEnum):
    NONE = 0
    CANCEL_MAKER = 1
    CANCEL_TAKER = 2


class Kind(IntEnum):
    FILL = 0
    REST = 1
    CANCEL = 2
    REJECT = 3
    REDUCE = 4
    EXPIRE = 5
    LEVEL = 6
    ORDER = 7
    STP_MAKER = 8
    STP_TAKER = 9
    SNAPSHOT = 10
    IMPORT = 11
    REPLACE = 12
    MASS_CANCEL = 13
    MASS_PREVIEW = 14


class Reject(IntEnum):
    NONE = 0
    ID = 1
    PRICE = 2
    QUANTITY = 3
    DUPLICATE = 4
    ABSENT = 5
    POLICY = 6
    POST = 7
    REDUCE = 8
    BANK = 9
    OWNER = 10
    FOK = 11
    OPCODE = 12
    DISPLAY = 13
    IMPORT = 14
    CROSSED = 15


@dataclass(frozen=True)
class Geometry:
    """Dimensions of one compiled matcher instance."""

    banks: int
    prices: int
    slots: int
    quantity_width: int
    command_depth: int
    event_depth: int
    sequence_width: int = 8
    owner_width: int = 8
    tag_width: int = 16

    def __post_init__(self):
        """Validate geometry at the software boundary before packing any values."""
        for field in fields(self):
            if getattr(self, field.name) < 1:
                raise ValueError(f"{field.name} must be positive")

    @property
    def bank_width(self):
        """Width includes one explicitly invalid bank code."""
        return self.banks.bit_length()

    @property
    def price_width(self):
        """A level query can encode a price beyond the grid."""
        return self.prices.bit_length()

    @property
    def id_width(self):
        """An identifier field includes an invalid sentinel code."""
        return self.slots.bit_length()

    @property
    def level_width(self):
        """Accommodate the sum of every individual quantity in the book."""
        return self.quantity_width + self.id_width

    @property
    def max_quantity(self):
        """Highest valid single-order quantity."""
        return (1 << self.quantity_width) - 1

    @property
    def sequence_mask(self):
        """Wrap command and event counters at the wire's finite width."""
        return (1 << self.sequence_width) - 1

    def command_layout(self):
        """Expose the command payload layout in descending bit order."""
        return (
            ("operation", 3), ("tif", 2), ("stp", 2), ("side", 1),
            ("order_id", self.id_width), ("price", self.price_width),
            ("quantity", self.quantity_width), ("display", self.quantity_width),
            ("import_visible", self.quantity_width), ("owner", self.owner_width),
        )

    def result_layout(self):
        """Expose the result payload layout in descending bit order."""
        return (
            ("kind", 4), ("last", 1), ("side", 1), ("maker", self.id_width),
            ("price", self.price_width), ("quantity", self.quantity_width),
            ("remaining", self.quantity_width), ("total", self.quantity_width),
            ("visible", self.quantity_width), ("peak", self.quantity_width),
            ("owner", self.owner_width), ("refreshed", 1),
            ("level_total", self.level_width), ("level_visible", self.level_width),
            ("level_count", self.id_width), ("reason", 4),
        )

    def arguments(self):
        """Supply exact geometry overrides to the simulation driver."""
        return {
            "BANKS": self.banks,
            "PRICE_LEVELS": self.prices,
            "ORDER_SLOTS": self.slots,
            "QTY_W": self.quantity_width,
            "OWNER_W": self.owner_width,
            "TAG_W": self.tag_width,
            "SEQ_W": self.sequence_width,
            "CMD_DEPTH": self.command_depth,
            "EVENT_DEPTH": self.event_depth,
        }


PROFILES = {
    "default": Geometry(3, 12, 12, 16, 4, 4),
    "odd": Geometry(5, 7, 9, 12, 3, 2, owner_width=4),
    "tiny": Geometry(1, 1, 3, 4, 1, 1, sequence_width=4, owner_width=2),
}


def check_unsigned(value, width, label):
    """Forbid truncation and negative values at the packed ABI boundary."""
    if not isinstance(value, int) or not 0 <= value < (1 << width):
        raise ValueError(f"{label}={value!r} is outside {width} unsigned bits")
    return int(value)


def pack(record, layout):
    """Pack left-to-right fields from the dataclass in the layout."""
    result = 0
    for name, width in layout:
        result = (result << width) | check_unsigned(getattr(record, name), width, name)
    return result


def unpack(payload, layout):
    """Decode fields from a width-checked packed word."""
    check_unsigned(payload, sum(width for _, width in layout), "payload")
    result = {}
    for name, width in reversed(layout):
        result[name] = payload & ((1 << width) - 1)
        payload >>= width
    return result


@dataclass(frozen=True)
class Command:
    bank: int = 0
    tag: int = 0
    operation: int = Operation.NEW
    tif: int = TimeInForce.GTC
    stp: int = Prevention.NONE
    side: int = 0
    order_id: int = 0
    price: int = 0
    quantity: int = 1
    display: int = 0
    import_visible: int = 0
    owner: int = 0

    def payload(self, geometry):
        """Validate header and pack the transaction payload."""
        check_unsigned(self.bank, geometry.bank_width, "bank")
        check_unsigned(self.tag, geometry.tag_width, "tag")
        return pack(self, geometry.command_layout())

    @classmethod
    def decode(cls, geometry, bank, tag, payload):
        """Decode a captured or synthetic input command under this geometry."""
        check_unsigned(bank, geometry.bank_width, "bank")
        check_unsigned(tag, geometry.tag_width, "tag")
        return cls(bank=bank, tag=tag, **unpack(payload, geometry.command_layout()))


@dataclass(frozen=True)
class Result:
    kind: int
    last: bool = True
    side: int = 0
    maker: int = 0
    price: int = 0
    quantity: int = 0
    remaining: int = 0
    total: int = 0
    visible: int = 0
    peak: int = 0
    owner: int = 0
    refreshed: bool = False
    level_total: int = 0
    level_visible: int = 0
    level_count: int = 0
    reason: int = Reject.NONE

    def payload(self, geometry):
        """Encode the complete result, including the zero-value padding."""
        return pack(self, geometry.result_layout())

    @classmethod
    def reject(cls, reason):
        """Construct the canonical non-mutating rejection result."""
        return cls(Kind.REJECT, reason=reason)


@dataclass(frozen=True)
class Event:
    bank: int
    tag: int
    command_sequence: int
    event_sequence: int
    order_id: int
    result: Result


@dataclass(frozen=True)
class Resting:
    side: int
    price: int
    total: int
    visible: int
    peak: int
    owner: int
    time_priority: int


def first_slice(quantity, peak):
    """A zero peak denotes a fully displayed order."""
    return min(quantity, peak) if peak else quantity


class ReserveBook:
    """Order oracle with unbounded arrival markers and independent sorting."""

    def __init__(self, geometry):
        """Initialize empty state for one instrument."""
        self.geometry = geometry
        self.orders = {}
        self._next_time = 0
        self.staged = None
        self.staged_expected = 0

    def _time(self):
        """Assign a distinct monotone age to a rest or a refresh."""
        previous = self._next_time
        self._next_time += 1
        return previous

    @staticmethod
    def _crossed(order, command):
        """Opposite-side limits include execution at exactly the maker price."""
        if order.side == command.side:
            return False
        if command.side:
            return order.price >= command.price
        return order.price <= command.price

    def _makers(self, command):
        """Rank current eligible makers, including newly refreshed orders."""
        direction = -1 if command.side else 1
        return sorted(
            ((key, row) for key, row in self.orders.items() if self._crossed(row, command)),
            key=lambda item: (direction * item[1].price, item[1].time_priority),
        )

    def _level(self, command):
        """Return aggregate visible, total and orders for a single side and tick."""
        if command.price >= self.geometry.prices:
            return [Result.reject(Reject.PRICE)]
        level = [
            row for row in self.orders.values()
            if row.side == command.side and row.price == command.price
        ]
        return [Result(
            Kind.LEVEL,
            side=command.side,
            price=command.price,
            level_visible=sum(row.visible for row in level),
            level_total=sum(row.total for row in level),
            level_count=len(level),
        )]

    def _existing(self, command):
        """Enforce owner authorization and carry existing priority across reduce."""
        order = self.orders.get(command.order_id)
        if order is None:
            return [Result.reject(Reject.ABSENT)]
        if order.owner != command.owner:
            return [Result.reject(Reject.OWNER)]
        base = {"side": order.side, "price": order.price, "owner": order.owner}
        if command.operation == Operation.CANCEL:
            del self.orders[command.order_id]
            return [Result(Kind.CANCEL, quantity=order.total, **base)]
        if command.operation == Operation.ORDER:
            return [Result(
                Kind.ORDER, total=order.total, visible=order.visible, peak=order.peak, **base,
            )]
        if not 0 < command.quantity < order.total:
            return [Result.reject(Reject.REDUCE)]
        remaining = command.quantity
        visible = min(order.visible, remaining)
        self.orders[command.order_id] = replace(order, total=remaining, visible=visible)
        return [Result(
            Kind.REDUCE, quantity=order.total - remaining, total=remaining,
            visible=visible, peak=order.peak, **base,
        )]

    def _replace(self, command):
        """Amend an owned order in place. A price, size or display change may lose queue priority."""
        oid = command.order_id
        if oid >= self.geometry.slots:
            return [Result.reject(Reject.ID)]
        current = self.orders.get(oid)
        if current is None:
            return [Result.reject(Reject.ABSENT)]
        if current.owner != command.owner:
            return [Result.reject(Reject.OWNER)]
        if (command.side, command.stp, command.import_visible) != (current.side, 0, 0):
            return [Result.reject(Reject.POLICY)]
        if command.price >= self.geometry.prices:
            return [Result.reject(Reject.PRICE)]
        if not command.quantity:
            return [Result.reject(Reject.QUANTITY)]
        if command.display > command.quantity and command.display != current.peak:
            return [Result.reject(Reject.DISPLAY)]
        if self._makers(command):
            return [Result.reject(Reject.POST)]

        renewed = (
            command.price != current.price or command.display != current.peak
            or command.quantity > current.total
        )
        rank = sum(row.time_priority < current.time_priority for row in self.orders.values())
        revised = replace(
            current, price=command.price, total=command.quantity, peak=command.display,
            visible=first_slice(command.quantity, command.display) if renewed
                    else min(current.visible, command.quantity),
            time_priority=self._time() if renewed else current.time_priority,
        )
        self.orders[oid] = revised
        return [Result(
            Kind.REPLACE, side=revised.side, maker=oid, price=revised.price,
            quantity=current.total, remaining=current.visible, total=revised.total,
            visible=revised.visible, peak=revised.peak, owner=revised.owner,
            refreshed=renewed, level_count=rank,
        )]

    @staticmethod
    def _belongs(command, row):
        if row.owner != command.owner:
            return False
        if command.stp == Scope.OWNER:
            return True
        if row.side != command.side:
            return False
        if command.stp == Scope.SIDE:
            return True
        if command.stp == Scope.LEVEL:
            return row.price == command.price
        if command.side:
            return row.price <= command.price
        return row.price >= command.price

    def _mass_operation(self, command):
        """Return exact owner-filtered rows and authoritative size totals."""
        if (command.order_id or command.quantity or command.display or command.import_visible
                or (command.stp == Scope.OWNER and command.side)
                or (command.stp in (Scope.OWNER, Scope.SIDE) and command.price)):
            return [Result.reject(Reject.POLICY)]
        if command.price >= self.geometry.prices:
            return [Result.reject(Reject.PRICE)]
        mutate = command.tif == Management.CANCEL
        kind = Kind.MASS_CANCEL if mutate else Kind.MASS_PREVIEW
        rows = sorted(
            ((key, row) for key, row in self.orders.items() if self._belongs(command, row)),
            key=lambda item: item[1].time_priority,
        )
        results = []
        for index, (oid, row) in enumerate(rows):
            results.append(Result(
                kind, last=False, side=row.side, maker=oid, price=row.price,
                total=row.total, visible=row.visible, peak=row.peak, owner=row.owner, level_count=index,
            ))
            if mutate:
                del self.orders[oid]
        results.append(Result(
            kind, owner=command.owner, level_count=len(rows),
            level_total=sum(row.total for _, row in rows),
            level_visible=sum(row.visible for _, row in rows),
        ))
        return results

    def _snapshot(self):
        """Report a finite ordered image followed by the authoritative row count."""
        image = sorted(self.orders.items(), key=lambda item: item[1].time_priority)
        rows = [
            Result(
                Kind.SNAPSHOT, last=False, side=order.side, maker=key, price=order.price,
                total=order.total, visible=order.visible, peak=order.peak,
                owner=order.owner, level_count=rank,
            )
            for rank, (key, order) in enumerate(image)
        ]
        rows.append(Result(Kind.SNAPSHOT, level_count=len(image)))
        return rows

    def _import_row(self, command):
        """Check one candidate row against the shadow image, with no live mutation."""
        if len(self.staged) >= self.staged_expected:
            return Result.reject(Reject.IMPORT)
        if command.order_id >= self.geometry.slots:
            return Result.reject(Reject.ID)
        if command.order_id in self.staged:
            return Result.reject(Reject.DUPLICATE)
        if command.price >= self.geometry.prices:
            return Result.reject(Reject.PRICE)
        if not command.quantity:
            return Result.reject(Reject.QUANTITY)
        visible = command.import_visible
        if not 0 < visible <= first_slice(command.quantity, command.display):
            return Result.reject(Reject.DISPLAY)
        if command.display == 0 and visible != command.quantity:
            return Result.reject(Reject.DISPLAY)
        if any(self._crossed(row, command) for row in self.staged.values()):
            return Result.reject(Reject.CROSSED)
        row = Resting(
            command.side, command.price, command.quantity, visible,
            command.display, command.owner, self._time(),
        )
        self.staged[command.order_id] = row
        return Result(
            Kind.IMPORT, side=row.side, maker=command.order_id, price=row.price,
            total=row.total, visible=row.visible, peak=row.peak, owner=row.owner,
            level_count=len(self.staged),
        )

    def _restore(self, command):
        """Open, stage, or atomically replace a complete ordered book image."""
        if command.tif == ImportPart.BEGIN:
            if self.staged is not None:
                return [Result.reject(Reject.IMPORT)]
            if command.order_id > self.geometry.slots:
                return [Result.reject(Reject.ID)]
            self.staged = {}
            self.staged_expected = command.order_id
            return [Result(Kind.IMPORT)]
        if self.staged is None:
            return [Result.reject(Reject.IMPORT)]
        if command.tif == ImportPart.ROW:
            return [self._import_row(command)]
        count = len(self.staged)
        if command.tif == ImportPart.COMMIT:
            if count != self.staged_expected:
                return [Result.reject(Reject.IMPORT)]
            self.orders = self.staged
        self.staged = None
        return [Result(Kind.IMPORT, level_count=count)]

    def _check_new(self, command):
        """Perform an admission check without any book changes."""
        if command.price >= self.geometry.prices:
            return Reject.PRICE
        if not command.quantity:
            return Reject.QUANTITY
        if command.stp not in tuple(Prevention):
            return Reject.POLICY
        if command.tif == TimeInForce.FOK and command.stp == Prevention.CANCEL_TAKER:
            return Reject.POLICY
        if command.display > command.quantity:
            return Reject.DISPLAY
        if command.order_id in self.orders:
            return Reject.DUPLICATE
        makers = self._makers(command)
        if command.tif == TimeInForce.POST and makers:
            return Reject.POST
        if command.tif == TimeInForce.FOK:
            eligible = [
                row.total for _, row in makers
                if command.stp != Prevention.CANCEL_MAKER or row.owner != command.owner
            ]
            if sum(eligible) < command.quantity:
                return Reject.FOK
        return Reject.NONE

    def _match_one(self, command, maker, order, taker_left):
        """Commit one match and append refreshes at the back of their price."""
        fill = min(taker_left, order.visible)
        taker_left -= fill
        resting_left = order.total - fill
        visible = order.visible - fill
        refreshed = bool(resting_left and not visible)
        if refreshed:
            visible = first_slice(resting_left, order.peak)
        if resting_left:
            self.orders[maker] = replace(
                order, total=resting_left, visible=visible,
                time_priority=self._time() if refreshed else order.time_priority,
            )
        else:
            del self.orders[maker]
        return Result(
            Kind.FILL, last=(taker_left == 0), side=command.side,
            maker=maker, price=order.price, quantity=fill, remaining=taker_left,
            total=resting_left, visible=visible, peak=(order.peak if resting_left else 0),
            owner=order.owner, refreshed=refreshed,
        )

    def _new(self, command):
        """Match, prevent self trades, then rest or expire any incoming remainder."""
        rejection = self._check_new(command)
        if rejection != Reject.NONE:
            return [Result.reject(rejection)]

        taker_left = command.quantity
        events = []
        while taker_left:
            ranked = self._makers(command)
            if not ranked:
                break
            maker, order = ranked[0]
            if order.owner == command.owner and command.stp == Prevention.CANCEL_TAKER:
                events.append(Result(
                    Kind.STP_TAKER, side=command.side, maker=maker, price=order.price,
                    quantity=taker_left, total=order.total, visible=order.visible,
                    peak=order.peak, owner=order.owner,
                ))
                return events
            if order.owner == command.owner and command.stp == Prevention.CANCEL_MAKER:
                del self.orders[maker]
                events.append(Result(
                    Kind.STP_MAKER, last=False, side=command.side, maker=maker,
                    price=order.price, quantity=order.total, remaining=taker_left,
                    owner=order.owner,
                ))
                continue
            result = self._match_one(command, maker, order, taker_left)
            taker_left = result.remaining
            events.append(result)
        if not taker_left:
            return events
        if command.tif == TimeInForce.FOK:
            raise AssertionError("admitted FOK order left unfilled")
        if command.tif == TimeInForce.IOC:
            events.append(Result(
                Kind.EXPIRE, side=command.side, price=command.price,
                quantity=taker_left, owner=command.owner,
            ))
            return events
        order = Resting(
            command.side, command.price, taker_left,
            first_slice(taker_left, command.display), command.display,
            command.owner, self._time(),
        )
        self.orders[command.order_id] = order
        events.append(Result(
            Kind.REST, side=order.side, price=order.price, owner=order.owner,
            quantity=taker_left, remaining=taker_left, total=order.total,
            visible=order.visible, peak=order.peak,
        ))
        return events

    def _invariants(self):
        """Check all book levels for feasible amounts and uncrossed state."""
        bids, asks, ages = [], [], set()
        for order_id, order in self.orders.items():
            assert 0 <= order_id < self.geometry.slots, order_id
            assert 0 <= order.price < self.geometry.prices, order
            assert 0 < order.visible <= order.total <= self.geometry.max_quantity, order
            assert order.visible <= first_slice(order.total, order.peak), order
            assert order.time_priority not in ages, order
            ages.add(order.time_priority)
            (asks if order.side else bids).append(order.price)
        if bids and asks:
            assert max(bids) < min(asks), (bids, asks)

    def execute(self, command):
        """Dispatch a valid-bank command and validate conservation on the result."""
        before = self.orders.copy()
        if (command.operation not in tuple(Operation)
                or (command.operation == Operation.MANAGE and command.tif not in tuple(Management))):
            results = [Result.reject(Reject.OPCODE)]
        elif command.operation == Operation.IMPORT:
            results = self._restore(command)
        elif command.operation == Operation.SNAPSHOT:
            results = self._snapshot()
        elif self.staged is not None and command.operation not in (Operation.LEVEL, Operation.ORDER):
            results = [Result.reject(Reject.IMPORT)]
        elif command.operation == Operation.MANAGE:
            results = (self._replace(command) if command.tif == Management.REPLACE
                       else self._mass_operation(command))
        elif command.operation == Operation.LEVEL:
            results = self._level(command)
        elif command.order_id >= self.geometry.slots:
            results = [Result.reject(Reject.ID)]
        elif command.operation == Operation.NEW:
            results = self._new(command)
        else:
            results = self._existing(command)
        if results[0].kind == Kind.REJECT:
            assert before == self.orders
        elif command.operation == Operation.NEW:
            assert sum(
                result.quantity for result in results if result.kind != Kind.STP_MAKER
            ) == command.quantity
        elif results[0].kind == Kind.MASS_PREVIEW:
            assert before == self.orders
        elif results[0].kind == Kind.MASS_CANCEL:
            removed = {result.maker for result in results[:-1]}
            assert self.orders == {key: row for key, row in before.items() if key not in removed}
        assert [result.last for result in results] == [False] * (len(results) - 1) + [True]
        self._invariants()
        return results


class ReserveMatcher:
    """Group independent books and their command/event sequences."""

    def __init__(self, geometry):
        """Prepare dimensions and initialize the live state."""
        self.geometry = geometry
        self.reset()

    def reset(self):
        """Drop each book and reset every local sequence at a logic reset."""
        self.books = [ReserveBook(self.geometry) for _ in range(self.geometry.banks)]
        self.command_sequences = [0] * (self.geometry.banks + 1)
        self.event_sequences = [0] * (self.geometry.banks + 1)

    def execute(self, command):
        """Apply a command and return the instrument-independent tagged event list."""
        command.payload(self.geometry)
        lane = min(command.bank, self.geometry.banks)
        if lane == self.geometry.banks:
            results = [Result.reject(Reject.BANK)]
        else:
            results = self.books[lane].execute(command)
        command_sequence = self.command_sequences[lane]
        self.command_sequences[lane] = (command_sequence + 1) & self.geometry.sequence_mask
        events = []
        for result in results:
            event_sequence = self.event_sequences[lane]
            self.event_sequences[lane] = (event_sequence + 1) & self.geometry.sequence_mask
            events.append(Event(
                command.bank, command.tag, command_sequence, event_sequence,
                command.order_id, result,
            ))
        return events
