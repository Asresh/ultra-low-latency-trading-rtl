"""Compile masked flow policies into a transactional Ethernet frontend CSR plan."""

import argparse
from dataclasses import dataclass
from ipaddress import IPv4Network
import json
from pathlib import Path

from net_wire import Match, Rule, bounded


CSR_ID = 0x00
CSR_VERSION = 0x04
CSR_EPOCH = 0x08
CSR_INDEX = 0x10
CSR_WORD0 = 0x20
CSR_PUSH = 0x40
CSR_COMMIT = 0x44
CSR_SNAPSHOT = 0x48
CSR_CLEAR = 0x4c
CSR_STAT_INDEX = 0x50
CSR_STAT_LOW = 0x54
CSR_STAT_HIGH = 0x58
CSR_STAT_ID = 0x5c
IDENTITY = 0x45554634
ABI_MAJOR = 1
RULE_COUNT = 8
FIELDS = {
    "src_ip": 32, "dst_ip": 32, "src_port": 16, "dst_port": 16, "tci": 16, "tags": 2,
}


def numeric(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric rule field")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise ValueError(f"expected a number, received {value!r}")


def fit(value, width, label):
    return bounded(numeric(value), width, label)


def parse_match(field, value):
    width = FIELDS[field]
    if value == "*" or value is None:
        return Match()
    if field.endswith("_ip") and isinstance(value, str) and "." in value:
        network = IPv4Network(value, strict=False)
        return Match(int(network.network_address), int(network.netmask))
    if isinstance(value, dict):
        if set(value) != {"value", "mask"}:
            raise ValueError(f"{field} object must have value and mask")
        return Match(fit(value["value"],width,field), fit(value["mask"],width,field))
    return Match(fit(value,width,field), (1 << width)-1)


def parse_rule(row):
    if not isinstance(row, dict):
        raise ValueError("each rule must be an object")
    extra = set(row)-set(FIELDS)-{"enabled", "action", "queue"}
    if extra:
        raise ValueError(f"unknown rule fields {', '.join(sorted(extra))}")
    enabled = row.get("enabled",True)
    if not isinstance(enabled,bool):
        raise ValueError("enabled must be a boolean")
    action = row.get("action","pass")
    if action not in ("pass","drop"):
        raise ValueError("action must be pass or drop")
    queue = fit(row.get("queue", 0), 3, "queue")
    if action == "drop" and queue:
        raise ValueError("drop rules have no queue")
    return Rule(**{field:parse_match(field,row.get(field)) for field in FIELDS},
                enabled=enabled,drop=action=="drop",queue=queue)


def parse_policy(document):
    if not isinstance(document,dict) or set(document) != {"rules"}:
        raise ValueError("policy must be an object with one rules array")
    rows = document["rules"]
    if not isinstance(rows,list) or len(rows) > RULE_COUNT:
        raise ValueError("policy may include at most eight rules")
    return tuple(parse_rule(row) for row in rows)


def load_policy(path):
    return parse_policy(json.loads(Path(path).read_text()))


@dataclass(frozen=True)
class Access:
    write: bool
    address: int
    value: int = 0
    strobe: int = 15
    expect: int = None

    def __post_init__(self):
        bounded(self.address,8,"CSR address")
        if self.address % 4:
            raise ValueError("CSR access must be word-aligned")
        bounded(self.value,32,"write value")
        bounded(self.strobe,4,"write byte mask")
        if self.expect is not None:
            bounded(self.expect,32,"expected response")

    def json_object(self):
        result = {"op":"write" if self.write else "read","address":f"0x{self.address:02x}"}
        if self.write:
            result.update(value=f"0x{self.value:08x}", strobe=self.strobe)
        if self.expect is not None:
            result["expect"] = f"0x{self.expect:08x}"
        return result


def access_read(address, expect=None):
    return Access(False,address,strobe=0,expect=expect)


def access_write(address, value):
    return Access(True,address,value)


def policy_plan(rules, *, expected_epoch=None, verify_words=True):
    if len(rules) > RULE_COUNT:
        raise ValueError("too many rules")
    if expected_epoch is not None:
        bounded(expected_epoch,8,"prior epoch")
    yield access_read(CSR_ID,IDENTITY)
    yield access_read(CSR_VERSION,ABI_MAJOR << 16)
    yield access_read(CSR_EPOCH,expected_epoch)
    filled = tuple(rules) + (Rule(enabled=False),)*(RULE_COUNT-len(rules))
    for index, rule in enumerate(filled):
        yield access_write(CSR_INDEX,index)
        if verify_words:
            yield access_read(CSR_INDEX,index)
        bits = rule.encode()
        for word in range(8):
            value = (bits >> (32*word)) & 0xffffffff
            address = CSR_WORD0+4*word
            yield access_write(address,value)
            if verify_words:
                yield access_read(address,value)
        yield access_write(CSR_PUSH,1)
    yield access_write(CSR_COMMIT,1)
    yield access_read(CSR_EPOCH,None if expected_epoch is None else (expected_epoch+1)&255)


def snapshot_plan(indexes, *, clear=False):
    if len(set(indexes)) != len(indexes):
        raise ValueError("snapshot plan contains a duplicate counter")
    yield access_write(CSR_SNAPSHOT,1)
    yield access_read(CSR_STAT_ID)
    for index in indexes:
        bounded(index,5,"counter index")
        yield access_write(CSR_STAT_INDEX,index)
        yield access_read(CSR_STAT_LOW)
        yield access_read(CSR_STAT_HIGH)
    yield access_read(CSR_STAT_ID)
    if clear:
        yield access_write(CSR_CLEAR,1)


class ControlError(RuntimeError):
    pass


class Control:
    """Transport-agnostic master. The transport callable returns (status, value)."""

    def __init__(self, transport):
        self.transact = transport

    def execute(self, plan):
        for request in plan:
            status, response = self.transact(request)
            if status:
                raise ControlError(f"CSR 0x{request.address:02x} returned status {status}")
            bounded(response,32,"CSR response")
            if request.expect is not None and response != request.expect:
                raise ControlError(f"CSR 0x{request.address:02x} returned 0x{response:08x}, "
                                   f"expected 0x{request.expect:08x}")
            yield response

    def replace_policy(self, rules, *, verify_words=True):
        old_epoch, = self.execute([access_read(CSR_EPOCH)])
        replies = list(self.execute(policy_plan(
            rules,expected_epoch=old_epoch,verify_words=verify_words
        )))
        return replies[-1]

    def snapshot(self, indexes, *, clear=False):
        indexes = tuple(indexes)
        replies = list(self.execute(snapshot_plan(indexes)))
        opening, closing = replies[1], replies[-1]
        if opening != closing:
            raise ControlError("stats snapshot changed during indexed reads")
        counters = {}
        for i, index in enumerate(indexes):
            position = 2+i*3
            counters[index] = replies[position+1] | replies[position+2] << 32
        if clear:
            list(self.execute([access_write(CSR_CLEAR,1)]))
        return opening, counters


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    rule = commands.add_parser("rules")
    rule.add_argument("policy",type=Path)
    rule.add_argument("--epoch",type=numeric)
    rule.add_argument("--no-readback",action="store_true")
    stats = commands.add_parser("snapshot")
    stats.add_argument("indexes",type=numeric,nargs="+")
    stats.add_argument("--clear",action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "rules":
            plan = policy_plan(load_policy(args.policy),expected_epoch=args.epoch,
                               verify_words=not args.no_readback)
        else:
            plan = snapshot_plan(args.indexes,clear=args.clear)
        output = [item.json_object() for item in plan]
    except (OSError,ValueError) as error:
        parser.error(str(error))
    print(json.dumps(output,indent=2))


if __name__ == "__main__":
    main()
