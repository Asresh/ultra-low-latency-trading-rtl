# Ultra Low Latency Trading RTL

Sixteen independent FPGA hardware projects in the [RTL Projects Everyday](https://github.com/Asresh/RTL-Projects-Everyday) format. Each implemented day folder contains synthesizable SystemVerilog, self-checking verification, a Makefile, and a README with a waveform captured from simulation. Days 01–08 cover trading subsystems, Days 10–14 teach reusable building blocks, and Days 15–17 cover market-data distribution, order egress, and execution reconciliation.

| Day | Project | Design problem |
| --- | --- | --- |
| 01 | [Redundant MoldUDP64 / ITCH market data](day01-redundant_market_data/) | Reconstruct one ordered stream from two unreliable copies of the feed. Validate complete datagrams before exposing messages, suppress duplicates, and request missing sequence ranges. |
| 02 | [Banked price-time matching engine](day02-banked_matching_engine/) | Process instruments independently while preserving order and price-time priority within each book. Decide whether an order crosses, rests, expires, is rejected, or amends existing quantity. |
| 03 | [Order risk and execution reconciliation](day03-order_risk_gateway/) | Reserve per-account and per-instrument credit, then reconcile fills, cancellations, rejects and amendments in order. Prevent stale events from modifying a reused order slot. |
| 04 | [Ethernet / IPv4 / UDP front end](day04-ethernet_udp_frontend/) | Validate and classify received datagrams, transmit independently queued UDP payloads, and replay captured datagrams against a byte-level reference. |
| 05 | [Market-by-order state and quote intents](day05-market_state_quote_engine/) | Rebuild price levels from order updates and recovery snapshots, fence stale inputs, and derive bounded passive quote intents. |
| 06 | [Multiclock latency telemetry](day06-multiclock_latency_telemetry/) | Timestamp and correlate observations from unrelated clocks, then export histogram snapshots and triggered records without stopping the active bank. |
| 07 | [Nasdaq OUCH and SoupBinTCP session](day07-ouch_order_session/) | Encode and decode an official protocol subset, preserve committed order state, and reconcile session replay after transport loss. |
| 08 | [Host/FPGA DMA queues](day08-host_dma_queues/) | Service host submission and completion rings, transfer verified payloads in both directions, and preserve credits across backpressure and bus faults. |
| 09 | [Multi-venue smart order router prompt](prompt.md) | Design brief for a future project. |
| 10 | [Packet stream-width converter](day10-stream_width_converter/) | Pack and unpack byte lanes while preserving payload order, boundaries, and tags. |
| 11 | [Best quote selector](day11-best_quote_selector/) | Select one best bid and best ask from a small set of venues using deterministic ties and stale-quote handling. |
| 12 | [Message rate limiter](day12-message_rate_limiter/) | Admit messages with a per-key token bucket and test burst capacity, refill, and backpressure. |
| 13 | [Timestamped stream replayer](day13-timestamped_stream_replayer/) | Load a short event tape and replay it on a clock under pause and output stalls. |
| 14 | [Fixed-point notional calculator](day14-fixed_point_notional/) | Compute quantity times price, round fees and cash amounts, and check a cash limit. |
| 15 | [Multicast packet fanout](day15-multicast_packet_fanout/) | Distribute copies of normalized feed packets to independently backpressured consumers. |
| 16 | [Credit-aware order egress scheduler](day16-credit_order_scheduler/) | Schedule queued orders across sessions with finite credits and bounded priority for control traffic. |
| 17 | [Drop-copy reconciliation](day17-drop_copy_reconciliation/) | Compare normalized execution reports from an order session and its independent drop-copy stream. |

Days 10–14 each have at least 5,001 substantive source lines across RTL, verification, and tools. Comments, docstrings, blank lines, generated artifacts, and READMEs are excluded from the count. Python functions and SystemVerilog functions and tasks have explanations at their definitions.

## Run and inspect

Use Icarus Verilog, Python 3 and GNU Make for simulation. The complete verification flow uses the same OSS CAD Suite release as CI, `2026-11-22`. Put that suite's `bin` directory on your `PATH`. [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build)

```sh
make verify      # Inventory, simulation, lint, synthesis, formal and netlist checks
make test        # All scoreboard regressions and the risk/matcher lifecycle
make lint        # RTL and tops checked per project
make synth       # Generic synthesis and structural checks
make formal      # FIFO, arbiter, risk-ledger, and market-state properties
make netlist-check # Compare a synthesized gateway against the transaction oracle
make route       # Common ECP5 characterization of the original three cores
make inventory   # Source inventory and per-project minimums
make waveform    # Rerun the waveform benches and regenerate the README images
```

To run a single project, change into its day folder and run `make test`. Run `make waveform` there to regenerate its README image and leave the VCD under `build/`. The drawings show selected windows from the checked simulations. If you switch Icarus versions in the same checkout, use `make clean` before rebuilding so that `vvp` runs binaries compiled by the selected version.

## Design evidence

- [System boundaries and integration](docs/system-boundaries.md) shows how a market receiver, exchange book and trading-side risk ledger participate in different parts of the system.
- [Expansion verification](docs/expansion-verification.md) reports source size, executed tests, and implementation limits for Days 04–08.
- [Original core characterization](docs/performance.md) records the common ECP5 measurements for Days 01–03.
- [FPGA trading role alignment](docs/role-alignment.md) maps the projects to requirements from trading-firm job descriptions.
- [Research notes](docs/research.md) summarize 30 papers and preprints and link primary protocol and design references.

The timing reports characterize RTL cores in isolation. Physical interfaces, board bring-up and exchange connectivity fall outside the reference flow.
