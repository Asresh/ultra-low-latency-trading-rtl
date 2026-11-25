"""Create exact edit, input-lane and output CRC scoreboards."""
from argparse import ArgumentParser
from pathlib import Path
from random import Random
from zlib import crc32


def rows_to(path, rows):
    path.write_text("".join(" ".join(f"{item:x}" for item in row)+"\n" for row in rows))


def lane_word(data):
    return int.from_bytes(data, "little")


def input_words(data, width, offset):
    first = True
    while data:
        size = min(width-offset, len(data))
        # Dirty unused lanes expose edit engines that accidentally read them.
        word = b"\xee"*offset + data[:size] + b"\xee"*(width-offset-size)
        data = data[size:]
        yield lane_word(word), ((1 << size)-1) << offset, first, not data
        first, offset = False, 0


def evaluate(desc, words, in_width, cap, max_prefix):
    tag, declared, head, tail, count, prefix, offset, mask, patch, add_crc = desc
    trimmed = declared-head-tail
    output_length = trimmed+count+4*add_crc
    status = 0
    if declared > cap:
        status = 3
    elif not declared or trimmed < 0 or count > max_prefix or not 0 < output_length <= 0xffff:
        status = 4
    elif any((mask >> i) & 1 and offset+i >= trimmed for i in range(4)):
        status = 5
    received = bytearray()
    for idx, (value, keep, first, last) in enumerate(words):
        selected = [i for i in range(in_width) if (keep >> i) & 1]
        correct_shape = bool(
            selected and selected == list(range(selected[0], selected[-1]+1))
            and first == (idx == 0) and (first or selected[0] == 0)
            and (last or selected[-1] == in_width-1)
        )
        received.extend((value >> (8*i)) & 0xff for i in selected)
        if not status:
            status = 1 if not correct_shape else (3 if len(received) > min(cap,0xffff) else 0)
    if not status and len(received) != declared:
        status = 2

    body = b""
    if status == 0:
        retained = received[head:head+trimmed]
        for i in range(4):
            if (mask >> i) & 1:
                retained[offset+i] = (patch >> (8*i)) & 0xff
        body = prefix.to_bytes(max_prefix, "little")[:count] + retained
        if add_crc:
            body += crc32(body).to_bytes(4, "little")
    return (tag, min(len(received), 65535), len(body), status), body


def build(directory, iw, ow, capacity, max_prefix, seed, amount, length_boundary=False):
    rng = Random(seed)
    desc_rows, out_rows, reports = [], [], []

    def packet(data, tag, *, lane=0, head=0, tail=0, prefix=b"", crc=False,
               offset=0, mask=0, patch=0, declared=None, corrupt=None):
        desc = (tag, len(data) if declared is None else declared, head, tail,
                len(prefix), lane_word(prefix[:max_prefix]), offset, mask, patch, crc)
        words = list(input_words(data, iw, lane))
        if corrupt:
            value, keep, first, last = words[0]
            if corrupt == "bad_first":
                first = not first
            elif corrupt == "empty":
                keep = 0
            words[0] = value, keep, first, last
        result, body = evaluate(desc, words, iw, capacity, max_prefix)
        desc_rows.append((*desc,len(words)))
        desc_rows.extend(words)
        reports.append(result)
        if body:
            for value, keep, first, last in input_words(body, ow, 0):
                valid_bytes = bin(keep).count("1")
                mask = (1 << (8*valid_bytes))-1
                out_rows.append((value & mask, keep, first, last, tag))

    base = b"ABCDEFGH"[:capacity]
    packet(base, 0x19, lane=iw-1, head=1, prefix=b"Z", mask=1, patch=0x72, crc=True)
    packet(base, 0x19, tail=len(base), crc=True)
    packet(base, 0x19, head=len(base)+1)
    packet(base, 0x19, mask=8, offset=len(base))
    packet(base, 0x19, declared=len(base)-1)
    packet(base, 0x19, declared=capacity+1)
    packet(base, 0x19, lane=iw-1, corrupt="bad_first")
    packet(base, 0x19, corrupt="empty")
    packet(base, 0x19, head=len(base), prefix=b"X")
    if length_boundary:
        exact_prefix = 0xffff-len(base)-4
        packet(base, 0x20, prefix=bytes(exact_prefix), crc=True)
        packet(base, 0x21, prefix=bytes(exact_prefix+1), crc=True)
        packet(base, 0x22, prefix=bytes(0x1ffff), crc=True)
    for serial in range(amount):
        length = rng.randint(1, capacity)
        data = bytes(rng.randrange(256) for _ in range(length))
        head = rng.randrange(length+1)
        tail = rng.randrange(length-head+1)
        remain = length-head-tail
        crc = rng.randrange(2)
        prefix = bytes(rng.randrange(256) for _ in range(rng.randrange(max_prefix+1)))
        mask = 0
        at = 0
        if remain and rng.randrange(3):
            at = rng.randrange(remain)
            mask = rng.randrange(1 << min(4, remain-at))
        fault = serial % 17
        if fault == 0:
            head = length+1
        elif fault == 1:
            at, mask = remain+1, 1
        elif fault == 2:
            length = length+1 if length < capacity else length-1
        elif fault == 3:
            data += b"\xd1"*(capacity-len(data)+1)
        packet(data, rng.randrange(16), lane=rng.randrange(iw), head=head, tail=tail,
               prefix=prefix, crc=crc, offset=at, mask=mask, patch=rng.getrandbits(32),
               declared=length, corrupt="bad_first" if fault == 4 else None)
    directory.mkdir(parents=True, exist_ok=True)
    rows_to(directory/"commands.txt",desc_rows)
    rows_to(directory/"packets.txt",out_rows)
    rows_to(directory/"reports.txt",reports)
    rows_to(directory/"totals.txt",[(len(out_rows),len(reports))])


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--in-width", type=int, required=True)
    parser.add_argument("--out-width", type=int, required=True)
    parser.add_argument("--capacity", type=int, required=True)
    parser.add_argument("--prefix", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--frames", type=int, default=240)
    parser.add_argument("--length-boundary", action="store_true")
    args = parser.parse_args()
    build(args.directory,args.in_width,args.out_width,args.capacity,args.prefix,
          args.seed,args.frames,args.length_boundary)


if __name__ == "__main__":
    main()
