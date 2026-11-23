# Ultra Low Latency Trading RTL

Eight independent FPGA hardware projects in the [RTL Projects Everyday](https://github.com/Asresh/RTL-Projects-Everyday) format. Each day folder puts the synthesizable SystemVerilog, self-checking verification, Makefile, and design README together. The original three projects cover market-data recovery, venue matching, and risk. Days 04–08 extend the portfolio into transport, market state, measurement, order entry, and host/FPGA data movement.

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
```

To run a single project, change into its day folder and run `make test`. If you switch Icarus versions in the same checkout, use `make clean` before rebuilding so that `vvp` runs binaries compiled by the selected version. See the project README for waveforms, parameter limits, interface fields and golden-model checks.

## Design evidence

- [System boundaries and integration](docs/system-boundaries.md) shows how a market receiver, exchange book and trading-side risk ledger participate in different parts of the system.
- [Expansion verification](docs/expansion-verification.md) reports source size, executed tests, and implementation limits for Days 04–08.
- [Original core characterization](docs/performance.md) records the common ECP5 measurements for Days 01–03.
- [FPGA trading role alignment](docs/role-alignment.md) maps the projects to requirements from trading-firm job descriptions.
- [Research notes](docs/research.md) summarize 30 papers and preprints and link primary protocol and design references.

The timing reports characterize RTL cores in isolation. Physical interfaces, board bring-up and exchange connectivity fall outside the reference flow.
