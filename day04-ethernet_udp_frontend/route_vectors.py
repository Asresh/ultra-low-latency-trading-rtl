"""Build a model-checked drive tape for the IPv4 next-hop selector."""

import argparse
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from random import Random
from typing import Optional


def ip(text):
    return int(IPv4Address(text))


GW = ip("192.0.2.1")
HOST = ip("192.0.2.88")
LOCAL = ip("192.0.2.50")
OTHER = ip("198.51.100.20")


def usable(number):
    first = number >> 24
    return first not in (0, 127) and first < 224


@dataclass(frozen=True)
class Rule:
    row: int
    enabled: bool = True
    vid: Optional[int] = None
    destination: int = 0
    prefix: int = 0
    deny: bool = False
    gateway: int = 0

    def fields(self):
        return (
            self.row, int(self.enabled), int(self.vid is not None), self.vid or 0,
            self.destination, self.prefix, int(self.deny), self.gateway,
        )

    def network(self):
        return IPv4Network((self.destination, self.prefix), strict=True)

    def valid(self, rows):
        if self.row >= rows:
            return False
        if not self.enabled:
            return True
        if not 0 <= self.prefix <= 32 or self.vid == 4095:
            return False
        try:
            self.network()
        except ValueError:
            return False
        return (self.gateway == 0 or usable(self.gateway)) and not (self.deny and self.gateway)


@dataclass(frozen=True)
class Lookup:
    cookie: int
    destination: int
    vid: Optional[int] = None

    def fields(self):
        return int(self.vid is not None), self.vid or 0, self.destination, self.cookie


class RouteModel:
    """Maintain active and inactive route snapshots and an elastic result."""

    def __init__(self, rows):
        self.rows = rows
        self.row_miss = (1 << rows.bit_length()) - 1
        self.reset()

    def reset(self):
        self.active = {}
        self.shadow = {}
        self.epoch = 0
        self.output = None

    def decision(self, query):
        common = query.cookie, self.epoch, int(query.vid is not None), query.vid or 0
        if query.vid == 4095 or not usable(query.destination):
            return (3, *common, 0, self.row_miss)
        matches = [
            rule for rule in self.active.values()
            if rule.vid == query.vid and IPv4Address(query.destination) in rule.network()
        ]
        if not matches:
            return (1, *common, 0, self.row_miss)
        winner = min(matches, key=lambda rule: (-rule.prefix, rule.row))
        return (2 if winner.deny else 0, *common,
                0 if winner.deny else winner.gateway or query.destination, winner.row)

    def edge(self, reset=False, lookup=None, write=None, commit=False, ready=True):
        """Transfer with the table that existed before this rising edge."""
        input_ready = not reset and (self.output is None or ready)
        config_ready = not reset and not commit
        error = False
        if reset:
            self.reset()
        else:
            if input_ready:
                self.output = None if lookup is None else self.decision(lookup)
            if commit:
                self.active, self.shadow = self.shadow, self.active
                self.epoch = (self.epoch + 1) % 256
            elif write is not None:
                if not write.valid(self.rows):
                    error = True
                elif write.enabled:
                    self.shadow[write.row] = write
                else:
                    self.shadow.pop(write.row, None)
        return input_ready, config_ready, error, self.epoch, self.output


class Campaign:
    def __init__(self, rows, seed):
        self.model = RouteModel(rows)
        self.random = Random(seed)
        self.lines = []
        self.counts = {name: 0 for name in (
            "lookups", "writes", "errors", "commits", "stalls", "overlap", "resets",
        )}
        self.statuses = set()
        self.cookie = 0

    def emit(self, reset=False, lookup=None, write=None, commit=False, ready=True):
        input_ready, config_ready, error, epoch, output = self.model.edge(
            reset, lookup, write, commit, ready,
        )
        self.counts["lookups"] += lookup is not None and input_ready
        self.counts["writes"] += write is not None and config_ready
        self.counts["errors"] += error
        self.counts["commits"] += commit and not reset
        self.counts["resets"] += reset
        self.counts["stalls"] += lookup is not None and not input_ready and not reset
        self.counts["overlap"] += lookup is not None and input_ready and commit
        if output is not None:
            self.statuses.add(output[0])
        drive = (
            int(reset), int(lookup is not None), *(lookup.fields() if lookup else (0,) * 4),
            int(write is not None), *(write.fields() if write else (0,) * 8),
            int(commit), int(ready),
        )
        sample = (
            int(input_ready), int(config_ready), int(commit and not reset), int(error),
            epoch, int(output is not None), *(output or (0,) * 7),
        )
        self.lines.append(" ".join(f"{value:x}" for value in (*drive, *sample)))
        return input_ready, config_ready

    def query(self, destination, vid=None):
        self.cookie = (self.cookie + 1) % 65536
        return Lookup(self.cookie, destination, vid)

    def known(self):
        self.emit(reset=True)
        self.emit(write=Rule(3, vid=24, destination=HOST, prefix=32, deny=True))
        self.emit(write=Rule(4, vid=24, destination=HOST, prefix=32, gateway=GW))
        self.emit(write=Rule(1, vid=24, destination=ip("192.0.2.0"), prefix=24))
        self.emit(write=Rule(0, vid=24, gateway=GW))
        self.emit(write=Rule(2, destination=ip("198.51.100.0"), prefix=24))
        self.emit(write=Rule(4, vid=24, destination=LOCAL, prefix=24))
        self.emit(write=Rule(self.model.rows, enabled=False))
        self.emit(write=Rule(1, vid=4095, prefix=0))
        self.emit(write=Rule(1, vid=24, prefix=33))
        self.emit(commit=True, write=Rule(3, enabled=False), lookup=self.query(LOCAL, 24))
        self.emit(write=Rule(3, enabled=False), lookup=self.query(HOST, 24))
        pending = self.query(OTHER, 24)
        self.emit(commit=True, lookup=pending, ready=False)
        self.emit(lookup=pending)
        self.emit(commit=True, lookup=self.query(OTHER))
        self.emit(lookup=self.query(OTHER, 24))
        self.emit(lookup=self.query(LOCAL, 24))
        self.emit(lookup=self.query(OTHER))
        self.emit(lookup=self.query(ip("239.1.1.1")))
        self.emit(lookup=self.query(OTHER, 4095))
        self.emit()

    def random_rule(self):
        r = self.random
        prefix = r.choice((0, 8, 16, 24, 28, 32, 34))
        source = r.choice((OTHER, LOCAL, HOST, 0, r.getrandbits(32)))
        address = source & (0xFFFF_FFFF << max(0, 32 - prefix))
        if r.randrange(16) == 0:
            address ^= 1
        return Rule(
            r.randrange(self.model.rows + 2), r.randrange(5) != 0,
            r.choice((None, 0, 24, 4095)), address, prefix,
            r.randrange(4) == 0, r.choice((0, GW, ip("224.1.1.1"))),
        )

    def stress(self, cycles):
        r = self.random
        lookup = None
        write = None
        for _ in range(cycles):
            reset = r.randrange(270) == 0
            if lookup is None and r.randrange(4):
                lookup = self.query(
                    r.choice((OTHER, LOCAL, HOST, GW, r.getrandbits(32))),
                    r.choice((None, 0, 24, 4095)),
                )
            if write is None and r.randrange(3) == 0:
                write = self.random_rule()
            lr, cr = self.emit(reset, lookup, write, r.randrange(9) == 0, r.randrange(3) != 0)
            if lr or reset:
                lookup = None
            if cr or reset:
                write = None
        self.emit(reset=True)

    def write(self, path):
        if self.statuses != {0, 1, 2, 3} or min(self.counts.values()) <= 0:
            raise ValueError(f"incomplete routing campaign {self.statuses}, {self.counts}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self.lines) + "\n", encoding="ascii")
        print(f"Generated {len(self.lines)} routing cycles: {self.counts}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tape", type=Path)
    parser.add_argument("--rows", type=int, choices=(8, 11), default=8)
    parser.add_argument("--seed", type=int, default=40402)
    parser.add_argument("--cycles", type=int, default=2400)
    args = parser.parse_args()
    campaign = Campaign(args.rows, args.seed)
    campaign.known()
    campaign.stress(args.cycles)
    campaign.write(args.tape)


if __name__ == "__main__":
    main()
