# Ultra Low Latency Trading RTL

Three independent FPGA hardware projects in the [RTL Projects Everyday](https://github.com/Asresh/RTL-Projects-Everyday) format. Each day folder puts the synthesizable SystemVerilog, self-checking verification, Makefile, and design README together. The designs contain 6,816 source lines across RTL, testbenches, models and host tools.

| Day | Project | Design problem |
| --- | --- | --- |
| 01 | [Redundant MoldUDP64 / ITCH market data](day01-redundant_market_data/) | Reconstruct one ordered stream from two unreliable copies of the feed. Validate complete datagrams before exposing messages, suppress duplicates, and request missing sequence ranges. |
| 02 | [Banked price-time matching engine](day02-banked_matching_engine/) | Process instruments independently while preserving order and price-time priority within each book. Decide whether an order crosses, rests, expires, is rejected, or amends existing quantity. |
| 03 | [Order risk and execution reconciliation](day03-order_risk_gateway/) | Reserve per-account and per-instrument credit, then reconcile fills, cancellations, rejects and amendments in order. Prevent stale events from modifying a reused order slot. |

## Run and inspect

Use Icarus Verilog, Python 3 and GNU Make for simulation. Install the [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build) for Verilator, Yosys and nextpnr.

```sh
make test        # All scoreboard regressions and the risk/matcher lifecycle
make lint        # RTL and tops checked per project
make synth       # Generic synthesis and structural checks
make formal      # FIFO/arbiter proofs and a bounded risk-ledger property check
make netlist-check # Compare a synthesized gateway against the transaction oracle
make route       # ECP5 clock and resource characterization
make inventory   # Reproducible source line counts
```

To run a single project, change into its day folder and run `make test`. See the corresponding README for waveforms, parameter limits, interface fields and golden-model checks.

## Design evidence

- [System boundaries and integration](docs/system-boundaries.md) shows how a market receiver, exchange book and trading-side risk ledger participate in different parts of the system.
- [Performance and verification](docs/performance.md) records the measured configurations, cycle behavior, tool versions, timing results and source line counts.
- [FPGA trading role alignment](docs/role-alignment.md) connects RTL, networking, verification, and timing decisions to roles at Optiver, Citadel Securities, Jane Street, IMC and DRW.
- [Research notes](docs/research.md) summarize 30 papers and preprints and link primary protocol and design references.

The timing reports characterize RTL cores in isolation. Physical interfaces, board bring-up and exchange connectivity fall outside the reference flow.
