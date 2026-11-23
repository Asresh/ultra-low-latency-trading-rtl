"""Build and verify synthetic, per-instrument snapshot restore plans."""

import argparse
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

from market_reference import Command, Config, MarketMode, Operation, Order, PROFILES
from market_replay import FeedRow, ReplaySession, read_rows, write_rows


CHECKPOINT_SCHEMA = "day05-restore-plan-v1"


class CheckpointError(ValueError):
    pass


def formatted(data) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def digest(data) -> str:
    return sha256(formatted(data)).hexdigest()


def field_int(obj: Mapping, field: str, maximum: int, context: str) -> int:
    value = obj.get(field)
    if type(value) is not int or not 0 <= value <= maximum:
        raise CheckpointError(f"{context}.{field} should be integer 0..{maximum}, received {value!r}")
    return value


@dataclass(frozen=True)
class Image:
    instrument: int
    source_epoch: int
    anchor: int
    orders: Tuple[Tuple[int, Order], ...]

    def payload(self) -> Dict:
        return {
            "instrument": self.instrument,
            "source_epoch": self.source_epoch,
            "anchor": self.anchor,
            "orders": [{"id": key, **asdict(order)} for key, order in self.orders],
        }

    @classmethod
    def from_order_rows(
        cls, instrument: int, source_epoch: int, anchor: int, rows: List[Dict],
        config: Config,
    ) -> "Image":
        context = f"instrument {instrument}"
        if type(rows) is not list or len(rows) > config.slots:
            raise CheckpointError(f"{context} has invalid active slot count")
        orders: Dict[int, Order] = {}
        for index, row in enumerate(rows):
            label = f"{context} order[{index}]"
            if type(row) is not dict or set(row) != {"id", "side", "price", "size"}:
                raise CheckpointError(f"{label} needs id, side, price, size")
            slot = field_int(row, "id", config.slots-1, label)
            price = field_int(row, "price", config.levels-1, label)
            size = field_int(row, "size", config.quantity_max, label)
            if size == 0:
                raise CheckpointError(f"{label} cannot encode a zero-sized live order")
            if type(row["side"]) is not bool:
                raise CheckpointError(f"{label}.side needs a Boolean")
            if slot in orders:
                raise CheckpointError(f"{label} repeats live slot {slot}")
            orders[slot] = Order(row["side"], price, size)
        return cls(instrument, source_epoch, anchor, tuple(sorted(orders.items())))

    def commands(self, cfg: Config) -> Iterator[Command]:
        sequence = self.anchor
        yield Command(instrument=self.instrument, op=Operation.SNAP_BEGIN, sequence=sequence)
        for key, order in self.orders:
            sequence = (sequence+1) & cfg.sequence_max
            yield Command(
                instrument=self.instrument, op=Operation.SNAP_ADD, sequence=sequence,
                order_id=key, side=order.side, price=order.price, quantity=order.size,
            )
        sequence = (sequence+1) & cfg.sequence_max
        yield Command(instrument=self.instrument, op=Operation.SNAP_END, sequence=sequence)

    def expected_sequence(self, cfg: Config) -> int:
        return (self.anchor + len(self.orders) + 2) & cfg.sequence_max


@dataclass(frozen=True)
class RestorePlan:
    profile: str
    images: Tuple[Image, ...]
    group_by_instrument: bool = False
    gap: int = 0
    initial_reset: bool = True

    @property
    def cfg(self) -> Config:
        return PROFILES[self.profile]

    def commands(self) -> Iterable[Command]:
        paths = [iter(image.commands(self.cfg)) for image in self.images]
        if self.group_by_instrument:
            for stream in paths:
                yield from stream
            return
        while paths:
            remaining = []
            for stream in paths:
                message = next(stream, None)
                if message is not None:
                    yield message
                    remaining.append(stream)
            paths = remaining

    def rows(self) -> Iterator[FeedRow]:
        cycle = 0
        if self.initial_reset:
            yield FeedRow(cycle, reset=True)
            cycle += 2
        for index, command in enumerate(self.commands()):
            wire = Command(
                instrument=command.instrument, op=command.op, sequence=command.sequence,
                order_id=command.order_id, side=command.side, price=command.price,
                quantity=command.quantity, tag=(index & self.cfg.mask(self.cfg.tag_width)),
            )
            yield FeedRow(cycle, command=wire)
            cycle += self.gap + 1

    def manifest(self) -> Dict:
        attrs = {
            "profile": self.profile,
            "group_by_instrument": self.group_by_instrument,
            "gap": self.gap,
            "initial_reset": self.initial_reset,
            "images": [image.payload() for image in self.images],
        }
        return {
            "schema": CHECKPOINT_SCHEMA,
            "data": attrs,
            "digest": digest(attrs),
            "expected": [
                {
                    "instrument": image.instrument, "orders": len(image.orders),
                    "next_sequence": image.expected_sequence(self.cfg),
                }
                for image in self.images
            ],
        }

    @classmethod
    def read_manifest(cls, manifest: Dict) -> "RestorePlan":
        if type(manifest) is not dict or set(manifest) != {"schema", "data", "digest", "expected"}:
            raise CheckpointError("manifest container requires schema, data, digest, expected")
        if manifest["schema"] != CHECKPOINT_SCHEMA:
            raise CheckpointError("unsupported manifest schema")
        data = manifest["data"]
        if type(data) is not dict or set(data) != {
            "profile", "group_by_instrument", "gap", "initial_reset", "images",
        }:
            raise CheckpointError("manifest data fields do not match schema")
        if digest(data) != manifest["digest"]:
            raise CheckpointError("manifest content digest does not match")
        profile = data["profile"]
        if type(profile) is not str or profile not in PROFILES:
            raise CheckpointError("manifest refers to an unknown RTL profile")
        for name in ("group_by_instrument", "initial_reset"):
            if type(data[name]) is not bool:
                raise CheckpointError(f"manifest {name} must be Boolean")
        gap = field_int(data, "gap", 20, "manifest")
        cfg = PROFILES[profile]
        if type(data["images"]) is not list or not data["images"]:
            raise CheckpointError("manifest contains no instrument images")
        images = []
        seen = set()
        for row in data["images"]:
            if type(row) is not dict or set(row) != {"instrument", "source_epoch", "anchor", "orders"}:
                raise CheckpointError("invalid manifest image fields")
            bank = field_int(row, "instrument", cfg.banks-1, "image")
            epoch = field_int(row, "source_epoch", cfg.mask(cfg.epoch_width), "image")
            anchor = field_int(row, "anchor", cfg.sequence_max, "image")
            if bank in seen:
                raise CheckpointError(f"manifest repeats bank {bank}")
            seen.add(bank)
            images.append(Image.from_order_rows(bank, epoch, anchor, row["orders"], cfg))
        plan = cls(profile, tuple(images), data["group_by_instrument"], gap, data["initial_reset"])
        if plan.manifest() != manifest:
            raise CheckpointError("manifest expected outcomes or canonical order differ")
        return plan


def parse_anchors(args: Iterable[str], cfg: Config) -> Dict[int, int]:
    result = {}
    for value in args:
        pair = value.split("=", 1)
        try:
            bank, anchor = (int(token, 10) for token in pair)
        except ValueError as exc:
            raise CheckpointError(f"anchor {value!r} must be BANK=SEQUENCE") from exc
        if not 0 <= bank < cfg.banks or not 0 <= anchor <= cfg.sequence_max:
            raise CheckpointError(f"anchor {value!r} outside instrument or sequence range")
        if bank in result:
            raise CheckpointError(f"duplicate anchor for instrument {bank}")
        result[bank] = anchor
    if not result:
        raise CheckpointError("provide at least one sequence anchor")
    return result


def from_inventory(
    data: Dict, profile: str, anchors: Dict[int, int],
    *, grouped: bool = False, gap: int = 0, reset: bool = True,
) -> RestorePlan:
    if not 0 <= gap <= 20:
        raise CheckpointError("gap must be between 0 and 20")
    cfg = PROFILES[profile]
    if not isinstance(data, dict) or type(data.get("instruments")) is not list:
        raise CheckpointError("inventory is missing an instruments array")
    inventory: Dict[int, Dict] = {}
    for raw in data["instruments"]:
        if type(raw) is not dict:
            raise CheckpointError("inventory instrument must be an object")
        bank = field_int(raw, "instrument", cfg.banks-1, "inventory")
        if bank in inventory:
            raise CheckpointError(f"duplicate inventory for instrument {bank}")
        inventory[bank] = raw
    images = []
    for bank, anchor in sorted(anchors.items()):
        if bank not in inventory:
            raise CheckpointError(f"missing inventory for requested instrument {bank}")
        row = inventory[bank]
        epoch = field_int(row, "epoch", cfg.mask(cfg.epoch_width), "inventory")
        images.append(Image.from_order_rows(bank, epoch, anchor, row.get("orders"), cfg))
    if not images:
        raise CheckpointError("no instruments selected")
    return RestorePlan(profile, tuple(images), grouped, gap, reset)


def assert_plan_rows(plan: RestorePlan, rows: Iterable[FeedRow]) -> Dict:
    # Compare source rows before interpreting their semantics. An omitted
    # command in a snapshot could otherwise form a valid smaller image.
    observed = tuple(rows)
    expected = tuple(plan.rows())
    if len(observed) != len(expected):
        raise CheckpointError(f"restore feed has {len(observed)} rows, expected {len(expected)}")
    for index, (received, wanted) in enumerate(zip(observed, expected)):
        if received != wanted:
            raise CheckpointError(f"restore feed diverges at row {index+1}, cycle {wanted.cycle}")
    runner = ReplaySession(plan.cfg)
    journal = list(runner.expand(observed))
    if any(record.response and record.response_ready and record.response["status"] for record in journal):
        raise CheckpointError("a snapshot command was rejected")
    selected = {image.instrument for image in plan.images}
    per_bank = []
    for bank, state in enumerate(runner.model.instruments):
        if bank not in selected:
            if state.mode != MarketMode.COLD or state.book.orders:
                raise CheckpointError(f"unselected instrument {bank} mutated during replay")
            continue
        image = next(item for item in plan.images if item.instrument == bank)
        if state.mode != MarketMode.LIVE:
            raise CheckpointError(f"instrument {bank} did not reach LIVE")
        if state.expected != image.expected_sequence(plan.cfg):
            raise CheckpointError(f"sequence mismatch for instrument {bank}")
        if tuple(sorted(state.book.orders.items())) != image.orders:
            raise CheckpointError(f"restored order table differs for instrument {bank}")
        per_bank.append({
            "instrument": bank, "committed_orders": len(state.book.orders),
            "new_epoch": state.epoch, "next_sequence": state.expected,
            "eligible": state.desired().active, "bbo": state.book.bbo().diagnostic(),
        })
    return {
        "profile": plan.profile, "cycles": runner.cycle,
        "restored": per_bank, "accepts": runner.counters["accepts"],
        "quote_transfers": runner.counters["quote_transfers"],
    }


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    tools = cli.add_subparsers(dest="operation", required=True)
    make = tools.add_parser("plan", help="turn an offline inventory into a canonical restore tape")
    make.add_argument("inventory", type=Path)
    make.add_argument("--anchor", action="append", required=True)
    make.add_argument("--profile", choices=PROFILES, default="default")
    make.add_argument("--grouped", action="store_true")
    make.add_argument("--omit-initial-reset", action="store_true")
    make.add_argument("--gap", type=int, default=0)
    make.add_argument("--feed", type=Path, required=True)
    make.add_argument("--manifest", type=Path, required=True)
    verify = tools.add_parser("verify", help="check that a tape restores exactly the selected inventory")
    verify.add_argument("manifest", type=Path)
    verify.add_argument("feed", type=Path)
    args = cli.parse_args()
    if args.operation == "plan":
        cfg = PROFILES[args.profile]
        raw = json.loads(args.inventory.read_text(encoding="utf-8"))
        plan = from_inventory(
            raw, args.profile, parse_anchors(args.anchor, cfg),
            grouped=args.grouped, gap=args.gap, reset=not args.omit_initial_reset,
        )
        rows = tuple(plan.rows())
        answer = assert_plan_rows(plan, rows)
        with args.feed.open("w", newline="", encoding="utf-8") as sink:
            write_rows(sink, rows)
        args.manifest.write_text(json.dumps(plan.manifest(), indent=2) + "\n", encoding="utf-8")
    else:
        plan = RestorePlan.read_manifest(json.loads(args.manifest.read_text(encoding="utf-8")))
        with args.feed.open(newline="", encoding="utf-8") as input_file:
            answer = assert_plan_rows(plan, read_rows(input_file, plan.cfg))
    print(json.dumps(answer, indent=2))


if __name__ == "__main__":
    main()
