# Day 13 · Timestamped Stream Replayer

A replay tape is a short list of words saying *send these bytes at this tick*. Load the list on the configuration pins, start playback, and observe the 64-bit output. The same tape can be played several times. Software can build the list from a simple CSV or an offline Ethernet packet capture.

The core is useful for traffic-generator and timestamp-monitor exercises. Feed a known pattern into a pipeline, introduce stalls, and check whether the measured delays match the expected schedule.

```mermaid
flowchart LR
  C["Host request<br/>APPEND, START, PAUSE, STOP"] --> V["Validate command"]
  V --> B["Immutable event tape"]
  V --> R["Status response"]
  B --> S["Compare next due tick to virtual time"]
  S --> O["One held output beat"]
  O --> D["Ready/valid sink"]
  D --> M["Transfer and lateness counts"]
  classDef input fill:#dbeafe,stroke:#2563eb,color:#10275b
  classDef memory fill:#fef3c7,stroke:#d97706,color:#533200
  classDef output fill:#dcfce7,stroke:#16a34a,color:#11432e
  class C,V,R input
  class B,S memory
  class O,D,M output
```

## Walkthrough

Suppose offset 0 holds four bytes for tag 7 and offset 3 holds eight bytes for tag 11. With an always-ready sink, the replayer fills its output slot at tick 0 and transfers that first word at tick 1. It fills the slot with the second word at tick 3 and transfers it at tick 4. If the first word stays visible through tick 5 because the sink is stalled, it transfers at tick 5. The second word can fill the slot on that same edge and then transfer at tick 6. Its reported due tick remains 3 and its offered tick is 5.

The timer counts virtual active edges, excluding pause and stop-pending edges. Pause freezes scheduling immediately and lets an already visible beat transfer. Resume re-enables scheduling on the following edge. Stop also leaves any currently visible beat stable until it transfers. A stop can end a stream in the middle of a packet, so a downstream packet buffer needs to flush any unfinished packet when observing `done`. Synchronous reset clears the tape, control response, output slot, and counters.

A repeated pass is anchored at `last transfer tick + repeat gap + 1`. Add the programmed offset of each event to that anchor to get its due tick. Equal offsets are processed in insertion order. The 48-bit virtual time and 32-bit statistics saturate at their maximum values.

## Simulation waveform

![A replayer beat stays valid with tag 0xA through a pause and stop until the sink accepts it](waveform.svg)

The checked depth-16 simulation captures a beat with tag `0xA` held while the sink is blocked. The pause at 625 ns stops virtual time at tick `0x2`. Resume and stop follow. The sink raises ready at 930 ns, and the rising edge at 935 ns transfers the beat, clears valid, and pulses done. Run `make waveform` to regenerate the image and audit the entire capture against the Python model.

## Connections

| Interface | Contract |
|---|---|
| Clock and reset | One positive-edge clock and synchronous active-high reset. |
| Configuration | `cfg_valid/cfg_ready` accepts a request once. Keep all request fields stable during a stall. |
| Response | `rsp_valid/rsp_ready` returns a four-bit status and final tape length. One shared response slot can block a new request. |
| Output | `m_valid/m_ready` transfers one 64-bit beat. `m_keep` selects contiguous low lanes, with lane 0 at bits 7:0, and `m_last` closes the packet. A 16-bit tag is constant within each packet. |
| Output timing | `m_pass` is one-based. `m_due` is the planned tick and `m_offered` is when the core first filled the output slot. Actual acceptance is sampled at `now_tick` on the transfer edge. |
| Lifecycle | `active` and `paused` describe the run. `done` pulses for one edge after normal completion or a graceful stop. |
| Counts | Beats, packet endings, late offers and complete passes count accepted handshakes. They clear on every accepted start and reset. |

| Code | Request | Data fields used |
|---|---|---|
| 0 | Append | `cfg_tick`, `cfg_data`, `cfg_keep`, `cfg_last`, `cfg_tag` |
| 1 | Clear | Removes programmed tape while idle. |
| 2 | Start | `cfg_repeats` must be nonzero. `cfg_gap` inserts repeat separation. |
| 3 | Pause | Valid while playing and neither paused nor stopping. |
| 4 | Resume | Valid while paused and neither idle nor stopping. |
| 5 | Stop | Valid in an active run. Let a held output transfer before terminating. |

The append checks run in capacity, timestamp, mask, then tag order. A partial low-lane mask is allowed on a final beat only. Appends are refused while active. Offsets must be monotonic and a start requires a nonempty tape ending at a packet boundary. Configure a positive `DEPTH`. The default 16-entry tape fits small simulation exercises, and regression also uses depths 1, 3, and 32.

| Status | Value | When reported |
|---|---:|---|
| OK | 0 | Accepted operation |
| BAD_OP | 1 | Opcode 6 or 7 |
| BUSY | 2 | Active clear, append, or start |
| FULL | 3 | Append exceeds tape depth |
| BAD_TIME | 4 | Append timestamp moves backwards |
| BAD_KEEP | 5 | Empty, gapped or partial nonfinal mask |
| BAD_TAG | 6 | Tag changes inside an open packet |
| EMPTY | 7 | Start without an image |
| OPEN_PACKET | 8 | Start before closing the final programmed packet |
| BAD_REPEAT | 9 | Start with zero passes |
| BAD_STATE | 10 | Pause, resume, or stop conflicts with lifecycle |

## Run and check

With Icarus Verilog, Python 3.9 or later, Verilator, and Yosys with the Slang plugin available:

```sh
make test
make lint
make synth
make formal   # Optional bounded checks with SymbiYosys and Boolector
```

The simulator saves every observed pin snapshot under `build/`. The independent Python oracle checks all accepted programming responses, output and counter values cycle by cycle over **31,220 clocks** across four geometries. Those campaigns transferred **5,579 beats** and **2,576 packet endings**. Three additional dense, sparse, and mixed profiles compare **112 accepted beats** against an analytical scheduler with periodic sink stalls. A separate limits bench injects near-maximum state to check 48-bit time and 32-bit counter saturation. The formal target checks output and response hold, tape stability, tag framing and counter behavior for 22 edges with arbitrary legal requests and sink stalls. Cover witnesses exercise valid replay after a malformed append, a second pass, a hold through pause, and normal end of two passes.

The core can offer one beat per clock within a pass when consecutive events are due and the sink stays ready. This is a simulated interface throughput. Default-depth generic Yosys synthesis produced 6,815 cells before FPGA mapping and is used as a structural check.

## Scheduled two-bank replay

Use [banked_stream_replayer.sv](banked_stream_replayer.sv) when the next replay tape needs to load while the current tape plays. The manager owns two independent copies of the original engine. One stream is selected at a time. An inactive writable bank can accept APPEND and CLEAR while the other bank supplies data. Once START reserves a complete tape, that tape is immutable until cancellation or retirement. A reservation can wait for a wall-clock release or for another run to finish.

```mermaid
flowchart LR
    H["Tape loader and run controls"] --> M["Ownership and reservation"]
    W["Wall clock"] --> M
    M --> A["Bank A<br/>tape and virtual scheduler"]
    M --> B["Bank B<br/>tape and virtual scheduler"]
    A --> S["Single ready/valid stream"]
    B --> S
    M --> C["Run starts and retirements"]
    classDef host fill:#dbeafe,stroke:#2563eb,color:#10275b
    classDef control fill:#ede9fe,stroke:#6d28d9,color:#2e1065
    classDef memory fill:#fef3c7,stroke:#d97706,color:#533200
    classDef output fill:#dcfce7,stroke:#16a34a,color:#11432e
    class H,W host
    class M control
    class A,B memory
    class S,C output
```

For hardware regression, run a baseline burst tape through a feed decoder while staging a capture of a second trading session. Arm the second tape with a release tick and use run IDs to correlate transmitted beats and timing measurements with the original capture. The manager allows software to prepare one image in the background and record how long a due reservation waited for the old run. A delayed sink can move the start later than the requested release. Use a margin between scheduled runs when wall-clock alignment matters. Virtual due and offered ticks remain relative to the started run, so a run's pause and sink backpressure can be analyzed separately.

### Ownership and scheduling

| Bank state | Meaning |
| --- | --- |
| 0 WRITABLE | Holds a retained tape or an empty image. Appends and clear use the legacy tape validation. |
| 1 ARMED | One scheduled reservation owns this bank. Appends and clear return BUSY. |
| 2 PLAYING | This bank owns the output, including any held final beat and the retirement edge. Appends and clear return BUSY. |

`cfg_bank` selects the addressed bank. The 64-bit `wall_tick` starts at zero and saturates. It advances through pauses, idle periods, and output stalls. START validates the addressed image and repeat count, reserves the single pending slot, and returns a fresh nonzero ID. The address must be writable and the pending slot empty. `cfg_release_tick` applies to the startup edge, and a release set in the past becomes eligible on the next edge after reservation. `cfg_repeats` and `cfg_gap` are latched with the reservation. Once a reservation is due, the manager can start it on the edge that retires the previous run. First output still follows the normal scheduler latency.

PAUSE, RESUME, and STOP require the active bank and its exact `cfg_run_id`. CANCEL (opcode 6) requires the armed bank and its exact ID. An accepted cancellation on the release edge prevents launch. INSPECT (opcode 7) reports a bank's state, run ID, image length, and whether the last packet is open. The command slot and reply use ready/valid handshakes. The response includes the echoed opcode and bank. Local management commands return a registered response. Native commands wait for the bank's response, echo its status, and capture its final tape length and framing state. The reported bank owner is the state at command acceptance, with successful START and CANCEL returning the resulting state. Another edge can change the live state before the reply transfers.

A stale ID for an existing armed or playing bank returns status 11. A command targeting a bank in the wrong state returns BAD_STATE (10). Status 12 reports ID exhaustion on a valid START. The default 32-bit counter increments on accepted reservations, including subsequently cancelled reservations. It stops at its maximum, so a delayed STOP cannot target a reused ID within that reset epoch. Hardware reset discards both tapes, the pending reservation, any held output, and all counters. A cross-reset host must clear its outstanding commands and assign its own epoch if it logs run IDs persistently.

### Stream and retirement

The output beat fields are the original replayer fields plus `m_bank` and `m_run_id`. These identify the owner while `active` is asserted. The virtual tick follows the owner and is zero when no run owns the stream. No ownership change can interrupt a held output beat. `run_started` pulses with the new owner and the absolute `run_start_tick`. `done` pulses with the retiring bank, ID, absolute retirement tick, and the final beat, packet, late-offer, and pass counts. The done record remains readable until the next retirement. These pulses are synchronous observations and have no ready input.

When the sink releases an already offered beat following STOP, the beat transfers before retirement. `done_stopped` identifies a stop-terminated run. `done_truncated` tells a downstream packet assembler to discard an unfinished packet from the retiring run. The flags are delivered with that run's ID before any beat from the next run can transfer. An assembler should process both fields when it observes `done`. A new run can announce its start on that same edge. Cumulative counters track starts, retirements, cancellations, and eligible reserved cycles spent waiting for an owner. The counters saturate at 2³²−1.

Defaults are 16 beats per bank and a 32-bit run ID. The manager synthesizes as an independent top and clocks both tape engines together. The measured interface behavior is simulation evidence. Mapped memory, physical clock rate, and asynchronous host adaptation depend on integration.

### Run the banked cases

```sh
make test-banked
make test-banked-bundle
make formal-banked
make synth-banked
make waveform-banked
```

Two seeded scoreboards compare 13,138 cycles, including 681 accepted beats, 26 retirement/start overlaps, and 210 staging writes while another bank plays. The formal harness checks held outputs and responses, owner stability, and retired run accounting for 18 edges. It covers handoff, packet truncation, stale IDs, and staging while active.

The bundle test verifies the manifest and TRP checksums, writes complete partitions into alternating banks, and captures actual RTL handshakes to `build/banked-accepted.csv`. The audit checks each accepted payload, mask, tag, relative due tick, bank and run ID against the originating partition. The same compiler accepts a bundle built with `replay_partition.py split`. To run a custom bundle, build its vectors with `replay_banked_bundle.py build DIRECTORY OUTPUT`, then supply `+vectors=OUTPUT +capture=PATH +bundle` to a bench built for its chosen depth and ID width. The compiler enforces a configurable simulation cycle budget. A run can start late while its tape loads or while the prior run holds the output.

![Inactive tape staging and safe run handoff](banked-waveform.svg)

In this simulation the first run starts at 165 ns and holds its first beat from 175 ns. The second bank accepts its own write and becomes reserved at 195 ns while the first is active. The pending release waits while the sink keeps ready low. At 345 ns, the held beat finally transfers. At 355 ns, the old run retires with its packet truncation flag and the second run becomes the owner. The second bank offers its first beat at 365 ns.

## Make your own replay tape

Create `build/packets.csv` with a header and exact decimal nanosecond timestamps. The payload field is unseparated hexadecimal.

```csv
time_ns,tag,payload
12.5,7,01020304
42.5,11,1122334455667788
```

```sh
python3 replay_cli.py compile-csv build/packets.csv build/image.trp --depth 16
python3 replay_cli.py inspect build/image.trp --repeats 2 --gap 3 --verbose
```

The default compiler uses a 100 MHz conversion rate, ceiling rounding, and one tick between serialized beats. It subtracts the first packet's timestamp before converting to virtual ticks. Set `--clock-hz`, `--lead`, `--beat-spacing`, `--packet-gap`, or `--speed` to change the schedule. Use `--collision fail` to reject times that the configured spacing cannot accommodate. `compile-pcap` accepts classic Ethernet PCAP with either byte order and either time resolution. It supports EtherType, VLAN, and time filters.

Use the following lines as `build/control.txt`:

```text
load image.trp
start 2 3 -> OK
await_done 60
```

```sh
python3 replay_cli.py script build/control.txt build/user.stim --depth 16
vvp build/depth-16.vvp +stim=build/user.stim +trace=build/user.trace
python3 replay_cli.py audit build/user.stim build/user.trace --depth 16
python3 replay_cli.py compare build/image.trp build/user.stim build/user.trace --repeats 2 --gap 3
python3 replay_cli.py metrics build/user.stim build/user.trace --verbose
python3 replay_cli.py export build/user.stim build/user.trace build/accepted.csv --reference accepted
```

A script can also use `sink off/on`, `responses off/on`, `pause`, `resume`, `stop`, `idle N`, `await_offer N`, and `reset`. Use an optional `-> STATUS` on configuration commands to assert acceptance or expected rejection. `transform` crops, rebases, and scales timestamps while preserving complete packets. If a capture is larger than the on-chip tape, `python3 replay_partition.py split build/image.trp build/parts --depth 16` writes independently loadable tape images and a checked manifest. Images are loaded and run one at a time by the host. The manifest retains their source offsets for comparison and merging offline.

A TRP image begins with a little-endian header containing magic `TRP1`, a 16-bit version, 16-bit record size, 32-bit record count, and CRC-32 of the records. Each 16-byte record has offset, data, tag, keep, and last fields in that order with widths 32, 64, 16, 8, and 8 bits. The last byte is either 0 or 1. The relative timestamps are 32-bit so the capture compiler rejects inputs that cannot fit in one virtual epoch.
