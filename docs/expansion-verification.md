# Expansion verification

Days 04–08 are independent SystemVerilog cores with self-checking simulations, host-side fixtures and per-project READMEs. The source inventory counts handwritten implementation, verification and tools. It excludes comments, blank lines, Python docstrings, generated fixtures and documentation. Run `make inventory` to reproduce the audit and enforce 5,001 lines per expansion project.

| Project | Synthesizable RTL | Verification and tools | Counted lines | Regression evidence |
| --- | ---: | ---: | ---: | --- |
| [04 · Ethernet / IPv4 / UDP](../day04-ethernet_udp_frontend/) | 1,408 | 3,729 | **5,137** | Two main seeds checked 734 RX results and 606 TX completions. The testbench also replayed 144 mixed-endian PCAPng packets through RTL. 62 Python tests passed. |
| [05 · Market state and quote intents](../day05-market_state_quote_engine/) | 865 | 4,348 | **5,213** | Four stress profiles accepted and acknowledged 24,232 commands. Journal/restore replay, 27,475 policy vectors, 69 Python tests and formal checks passed. |
| [06 · Multiclock latency telemetry](../day06-multiclock_latency_telemetry/) | 1,423 | 3,623 | **5,046** | 3,600 observations from four clock domains and 4,096 capacity observations. Histogram, trace, timebase, clock-domain FIFOs and AXI-Lite have separate scoreboards. 26 Python tests passed. |
| [07 · OUCH and SoupBinTCP](../day07-ouch_order_session/) | 1,647 | 3,431 | **5,078** | The three-profile RTL regression compared 1,238 byte-exact TX frames, 3,390 notices and 2,562 state checkpoints. Six byte-cut cases and 30 Python tests passed. |
| [08 · Host/FPGA DMA](../day08-host_dma_queues/) | 908 | 4,157 | **5,065** | Two 263-case AXI profiles, 127 FENCE operations under multi-queue contention and 14 independently audited integration completions. The host tests cover 192 generated requests and 37 unit tests. |

All five projects pass Verilator lint and Yosys structural synthesis. The [continuous integration workflow](../.github/workflows/rtl.yml) also checks the original three projects. The project READMEs list reproducible commands, seeds, interface formats and simulator output.

## Timing and boundaries

| Project | Measured result | Boundary |
| --- | --- | --- |
| 04 | 26 simulated cycles from first accepted MAC RX beat to metadata. Empty-body TX starts two cycles after its descriptor. | MAC/PHY adapter, FPGA timing and sustained wire throughput require a target platform. |
| 05 | One cycle to an unloaded ACK and three cycles to an unloaded quote. Default out-of-context ECP5 route reached **44.73 MHz**. | The route missed its 100 MHz constraint. It exposes the cost of the level read and policy datapath. |
| 06 | 4,096 observations over 4,681 offered capacity cycles. The unstalled correlator returns a result after one cycle. | Digital tests cover clock crossing order and skew in simulation. Board calibration, physical CDC analysis and routed timing are platform-specific. |
| 07 | Default out-of-context ECP5 route reached **25.99 MHz**. | The route missed its 100 MHz constraint. The critical path passes through RX assembly, decode and order-state lookup. TCP and exchange connectivity belong to the adapter. |
| 08 | Three-queue simulation saw at most two eligible grants bypassed and verified no retirement across 521 held-B cycles. | The design exposes an AXI memory interface. PCIe, cache coherency, driver integration and routed timing require a platform. |

The ECP5 results report throughput constraints for isolated default cores. The cycle metrics apply to the stated test profiles.
