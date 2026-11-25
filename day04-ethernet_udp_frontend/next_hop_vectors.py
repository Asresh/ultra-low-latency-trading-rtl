"""Build a route-to-neighbor transaction tape from an IP network oracle."""
from argparse import ArgumentParser
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
import random

from routed_link_fixture import multicast_mac


@dataclass(frozen=True)
class Row:
    vlan: int
    network: IPv4Network
    gateway: IPv4Address
    reject: bool = False


def row(vlan, network, gateway="0.0.0.0", reject=False):
    return Row(vlan, IPv4Network(network), IPv4Address(gateway), reject)


ROUTES = (
    row(-1, "0.0.0.0/0", "192.0.2.1"),
    row(-1, "203.0.113.0/24"),
    row(24, "0.0.0.0/0", "192.0.2.20"),
    row(24, "198.51.100.64/26", reject=True),
    row(0, "192.0.2.0/24"),
    row(-1, "203.0.113.0/24", "192.0.2.100"),
)


def explicit(fields):
    return " ".join(f"{int(field):x}" for field in fields) + "\n"


def expected(vlan, dest, group_ok, outcome):
    no_row = (1 << len(ROUTES).bit_length()) - 1
    if vlan == 4095 or (not dest.is_multicast and dest.packed[0] in (0, 127)) or dest.packed[0] >= 240:
        return 3, 0, 0, no_row, False
    if dest.is_multicast:
        if not group_ok:
            return 7, 0, 0, no_row, False
        return 0, int(dest), multicast_mac(dest), no_row, False
    choices = ((index, route) for index, route in enumerate(ROUTES)
               if route.vlan == vlan and dest in route.network)
    chosen = min(choices, key=lambda item: (-item[1].network.prefixlen, item[0]), default=None)
    if chosen is None:
        return 1, 0, 0, no_row, False
    index, selected = chosen
    if selected.reject:
        return 2, 0, 0, index, False
    gateway = int(selected.gateway) or int(dest)
    physical = 0x020000000000 | gateway if outcome == 0 else 0
    return (0, 4, 5, 6)[outcome], gateway, physical, index, True


def write_tapes(destination, seed, count):
    rng = random.Random(seed)
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / "rows.hex").open("w") as stream:
        for index, row_info in enumerate(ROUTES):
            stream.write(explicit((index, row_info.vlan != -1, max(0, row_info.vlan),
                                   row_info.network.network_address, row_info.network.prefixlen,
                                   row_info.gateway, row_info.reject)))

    strategic = [IPv4Address(value) for value in (
        "0.1.1.1", "127.0.0.1", "255.255.255.255", "192.0.2.42",
        "198.51.100.63", "198.51.100.64", "198.51.100.127", "198.51.100.128",
        "203.0.113.9", "203.0.114.9", "239.129.2.3",
    )]
    with (destination / "requests.hex").open("w") as stream:
        for tag in range(count):
            vlan = rng.choice([-1, -1, 0, 24, 24, 25, 4095])
            dest = rng.choice(strategic) if rng.randrange(5) else IPv4Address(rng.getrandbits(32))
            allowed = rng.randrange(2)
            neighbor_outcome = rng.choice((0, 0, 0, 1, 2, 3))
            status, gateway, physical, index, arp_query = expected(vlan, dest, allowed, neighbor_outcome)
            # Untagged inputs deliberately carry an ignored VID.
            request_vid = rng.randrange(4096) if vlan == -1 else vlan
            stream.write(explicit((tag, vlan != -1, request_vid, dest, allowed,
                                   status, gateway, physical, index, arp_query,
                                   neighbor_outcome, rng.randint(2, 45))))


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--count", default=600, type=int)
    args = parser.parse_args()
    if not 0 < args.count <= 1600:
        parser.error("count must be in 1..1600")
    write_tapes(args.directory, args.seed, args.count)


if __name__ == "__main__":
    main()
