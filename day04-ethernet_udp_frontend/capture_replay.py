"""Bundle a timed Ethernet capture with policy and an independent RTL RX oracle."""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import random
import zlib

from capture_fixtures import enhanced, interface, section, simple
from capture_io import CaptureError, check_output_paths, packets
from capture_study import Study, interface_id
from net_scenarios import DST, standard
from net_wire import Match, Result, Rule, to_beats
from rule_control import load_policy


REPLAY_FILES = (
    "rt_rules.txt", "rt_mac.txt", "rt_info.txt", "rt_payload.txt", "rt_counts.txt", "rt_report.json",
)


def stable_rules():
    return (
        Rule(dst_port=Match(1000,65535),drop=True),
        Rule(dst_ip=Match(DST,0xffffff00),dst_port=Match(18000,65535),queue=7),
        Rule(tags=Match(2,3),queue=3),
    )


def synthetic_input(seed):
    rng = random.Random(seed)
    data = section("<")+interface("<",name="A",scale=9,fcs=32)
    data += interface("<",name="B",scale=9,fcs=32)
    for index in range(144):
        size = rng.randrange(0,128)
        depth = index%3
        tags = tuple((0x88a8 if depth == 2 and i == 0 else 0x8100,rng.getrandbits(16))
                     for i in range(depth))
        item = standard(
            bytes(rng.randrange(256) for _ in range(size)),tags=tags,
            dst_port=(18000,18000,1000,43000)[index%4], options=b"\x01"*(4*(index%11)),
            udp_zero=index%9==0,ident=index,df=bool(index%2),
        )
        raw = item.serialize(fill=rng.randrange(256))
        if index%17 == 0 and item.payload and not item.udp_zero:
            start = len(item.l2())+len(item.options)+20+8
            raw = raw[:start]+bytes((raw[start]^0x80,))+raw[start+1:]
        crc = zlib.crc32(raw)^int(index%23==0)
        wire = raw+crc.to_bytes(4,"little")
        if index%18 == 0:
            data += enhanced("<",wire[:40],iface=index%2,ticks=6*index,wirelen=len(wire))
        elif index%25 == 0:
            data += simple("<",wire)
        else:
            data += enhanced("<",wire,iface=index%2,ticks=6*index)
    data += section(">")+interface(">",name="capture-2",scale=9,fcs=32)
    for index in range(8):
        raw = standard(b"mixed endian"+bytes([index])).serialize()
        data += enhanced(">",raw+zlib.crc32(raw).to_bytes(4,"little"),ticks=15*index)
    return data


def bundle(capture, directory, rules, *, clock=156250000, fcs="metadata",
           selected_interface=None, idle=0, strict=False):
    directory = Path(directory)
    directory.mkdir(exist_ok=True,parents=True)
    rules = tuple(rules)
    if len(rules) > 8:
        raise ValueError("bundle accepts no more than eight rules")
    padded = rules+(Rule(enabled=False),)*(8-len(rules))
    config, stimulus_path, meta_path, payload_path, counts_path, report_path = (
        directory / name for name in REPLAY_FILES
    )
    config.write_text("\n".join(f"{rule.encode():064x}" for rule in padded)+"\n")
    study = Study(rules,clock_hz=clock,fcs_mode=fcs,interface=selected_interface,
                  idle=idle,allow_zero=not strict)
    input_count = 0
    expected_count = 0
    body_count = 0
    with stimulus_path.open("w") as stimulus, meta_path.open("w") as meta, \
         payload_path.open("w") as payload:
        for frame in capture:
            for replay in study.add(frame):
                stimulus.write(f"{replay.cycle:d} {replay.capture_serial:d} {replay.beat.row()}\n")
                input_count += 1
                if replay.result is None:
                    continue
                result = replay.result
                value,mask = result.rx_info()
                meta.write(f"{replay.capture_serial:d} {value:096x} {mask:096x}\n")
                expected_count += 1
                if result.result == Result.OK and result.packet.payload:
                    for beat in to_beats(result.packet.payload):
                        payload.write(f"{replay.capture_serial:d} {beat.row()}\n")
                        body_count += 1
    counts_path.write_text(f"{input_count} {expected_count} {body_count}\n")
    report = study.report()
    report_path.write_text(json.dumps(report, indent=2)+"\n")
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    inputs = ap.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--capture", type=Path)
    inputs.add_argument("--synthetic", action="store_true")
    ap.add_argument("--directory",type=Path,required=True)
    ap.add_argument("--policy",type=Path)
    ap.add_argument("--clock-hz",type=int,default=156250000)
    ap.add_argument("--fcs",choices=("metadata","none","force","verify"))
    ap.add_argument("--interface",type=interface_id)
    ap.add_argument("--idle",type=int,default=0)
    ap.add_argument("--strict",action="store_true")
    ap.add_argument("--seed",type=int,default=4704)
    args = ap.parse_args()
    try:
        capture_file = args.capture
        outputs = [args.directory / name for name in REPLAY_FILES]
        if args.synthetic:
            capture_file = args.directory / "fixture.pcapng"
            outputs.append(capture_file)
        check_output_paths((args.capture, args.policy), outputs)
        rules = load_policy(args.policy) if args.policy else stable_rules()
        if args.synthetic:
            args.directory.mkdir(exist_ok=True,parents=True)
            capture_file.write_bytes(synthetic_input(args.seed))
        fcs_mode = args.fcs or ("verify" if args.synthetic else "metadata")
        outcome = bundle(packets(capture_file),args.directory,rules,clock=args.clock_hz,
                         fcs=fcs_mode,selected_interface=args.interface,idle=args.idle,
                         strict=args.strict)
    except (OSError,ValueError,CaptureError) as error:
        ap.error(str(error))
    print(f"Bundled {outcome['total']['emitted_packets']} packets across "
          f"{len(outcome['interfaces'])} interface(s).")


if __name__ == "__main__":
    main()
