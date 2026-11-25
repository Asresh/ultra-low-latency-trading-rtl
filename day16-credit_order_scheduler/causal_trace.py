"""Audit accepted egress, venue evidence, and terminal causal decisions."""
from argparse import ArgumentParser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Transaction:
    tag: int
    dependency: Optional[tuple]
    sent: Optional[int] = None
    acknowledged: Optional[tuple] = None
    verdict: Optional[int] = None
    waived: bool = False


def check(lines):
    orders = {}
    previous = [0, -1]
    transaction_count = 0
    waived = 0
    status_rank = {"I": 0, "T": 1, "A": 2, "D": 3}
    payload_fields = {"I": 3, "T": 2, "A": 1, "D": 2}
    for line_no, line in enumerate(lines, 1):
        event, *raw = line.split()
        if event not in status_rank or len(raw) != 4+payload_fields[event]:
            raise ValueError(f"line {line_no}: bad event layout")
        time, bank, epoch, sequence, *fields = map(int, raw)
        seen_at = [time, status_rank[event]]
        if time < 0 or seen_at <= previous:
            raise ValueError(f"line {line_no}: timestamp or same-edge event order")
        previous = seen_at
        key = bank, epoch, sequence

        if event == "I":
            tag, depends, parent = fields
            dependency = (bank, epoch, parent) if depends else None
            if key in orders or depends not in (0, 1) or (
                    dependency and (dependency not in orders or parent >= sequence)):
                raise ValueError(f"line {line_no}: invalid new dependency")
            orders[key] = Transaction(tag, dependency)
            continue

        order = orders.get(key)
        if event == "A":
            success, = fields
            if success not in (0, 1):
                raise ValueError(f"line {line_no}: malformed acknowledgment")
            if order and order.sent is not None and order.sent < time and order.acknowledged is None:
                order.acknowledged = (time, bool(success))
            continue
        if order is None or fields[0] != order.tag:
            raise ValueError(f"line {line_no}: completion or send has no matching accepted order")

        if event == "T":
            applicable = fields[1]
            if applicable not in (0, 1) or order.sent is not None or order.waived:
                raise ValueError(f"line {line_no}: duplicate or corrupt egress")
            if applicable:
                if order.verdict is not None:
                    raise ValueError(f"line {line_no}: terminal order attempted dispatch")
                if order.dependency is not None:
                    ack = orders[order.dependency].acknowledged
                    if ack is None or ack[0] >= time or not ack[1]:
                        raise ValueError(f"line {line_no}: dependency lacked earlier confirmed success")
                order.sent = time
                transaction_count += 1
            else:
                order.waived = True
                waived += 1
            continue

        code = fields[1]
        if order.verdict is not None or not 0 <= code <= 4:
            raise ValueError(f"line {line_no}: invalid terminal verdict")
        if code in (0, 1):
            if (order.sent is None or order.acknowledged is None or
                    order.acknowledged[0] >= time or order.acknowledged[1] != (code == 0)):
                raise ValueError(f"line {line_no}: venue disposition lacks matching evidence")
        elif order.sent is not None:
            raise ValueError(f"line {line_no}: locally rejected work was sent")
        order.verdict = code

    for key, row in orders.items():
        if row.verdict is None:
            raise ValueError(f"trace ended with unresolved order {key}")
        if row.verdict == 2 and (row.dependency is None or orders[row.dependency].verdict == 0):
            raise ValueError(f"dependent rejection {key} lacked a failed predecessor")
    return len(orders), transaction_count, waived


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    args = parser.parse_args()
    with args.trace.open() as handle:
        orders, sent, waived = check(handle)
    print(f"PASS causal trace: {orders} terminal orders, {sent} sent, {waived} revoked offers")


if __name__ == "__main__":
    main()
