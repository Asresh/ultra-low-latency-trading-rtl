# Verification, timing, and source inventory

The table below describes the expanded designs. The [original prototype measurements](https://github.com/Asresh/ultra-low-latency-trading-rtl/blob/80c7c41209814901a715ce0ee029ee4868b742cf/docs/performance.md) cover different cores and test conditions.

## Verification and measured cycles

| Design | Workload and check | Measurement |
| --- | --- | --- |
| Market data receiver | Two fault regression seeds compare 627 whole 588-bit events each and verify recovery requests, status, stalls and malformed packet atomicity. The default-size test checks 84 more events and exercises the exact packet and block limits. | A one-message, 41-byte datagram transferred a decoded event 15 cycles after its last beat in the small stress configuration, with six simultaneous accepted beats on both A/B ports. |
| Banked matcher | Four differential runs compare 17,055 commands and 18,026 events across three geometries. An independent order and priority model checks every command and event. Formal proves FIFO sequence and arbiter safety and fairness for the documented three-entry or three-lane instances. | Unloaded first event in 3 cycles. Balanced three-bank query traffic transferred 1.0000 event/cycle over the active output window versus 0.5007 event/cycle for a single-bank burst. |
| Risk gateway | Two parameter profiles check 7,410 decisions against an independent reservation and execution ledger. A synthesized-netlist run checks 205 additional transactions. The host reader independently validates CRC and sequence for every serialized response. Focused tests cover maxima, event and generation exhaustion, and audit policy under backpressure. | An unstalled request accepted at edge n commits and raises a reply after n+3; first reply transfer n+4. The single-issue ledger accepts at most one command every four clocks. |
| Cross-core lifecycle | Two account/side ledgers and a banked matcher. One venue fill becomes a maker and a taker execution update. The test verifies reject isolation, execution price versus limit price, cancel release, and stale generation after reuse. | 9 risk responses, 4 venue events, and explicit holds on both response streams. |

## Source size

The count includes SystemVerilog RTL and testbenches, formal harnesses, and Python models and host tools. It excludes comments, docstrings, blank lines, Makefiles, Markdown, and generated artifacts. `make inventory` enforces 2,000 source lines for each project. `python3 scripts/source_inventory.py --compare` additionally compares same-name source against the original release in a clone with history.

| Project | RTL | Verification / tools | Total | Added or changed since baseline |
| --- | ---: | ---: | ---: | ---: |
| day01-redundant_market_data | 1075 | 1131 | 2206 | 1938 |
| day02-banked_matching_engine | 613 | 1663 | 2276 | 2234 |
| day03-order_risk_gateway | 590 | 1744 | 2334 | 2334 |

## Routed clock and area

The common characterization uses the OSS CAD Suite 2026-11-22, Yosys 0.71+77 with Slang elaboration, nextpnr-ecp5, LFE5U-85F/CABGA381 speed grade 8, seed 1 and a single route thread. It maps each default core separately. The gateway numbers cover the ledger core; its optional audit tap and serializer are independently synthesized and exercised in simulation.

| Core | Seed | Target MHz | Routed MHz | Met | Comb sites | FFs | DSPs | EBRs |
| --- | ---: | ---: | ---: | :---: | ---: | ---: | ---: | ---: |
| redundant_market_data | 1 | 100.00 | 49.19 | No | 14075 | 7014 | 0 | 20 |
| banked_matching_engine | 1 | 100.00 | 73.91 | No | 10172 | 2973 | 0 | 0 |
| hft_order_risk_gateway | 1 | 100.00 | 81.50 | No | 4392 | 2897 | 4 | 0 |

The 100 MHz common characterization target is a tool constraint. Output reports retain the measured clock even if a core misses the target. Clock estimates exclude device I/O, global clock routing, MAC/PHY and board paths. One event per clock in a cycle simulation does not establish link throughput.

The receiver's routed path includes capture-bank selection and the reorder control carrying 64-bit sequence state. The matching path includes the best-price selection and per-order data feeding the event path. The gateway's long path runs through wide ledger arithmetic and the decision path. Registering the remaining ledger amount before commit improved its estimate from 78.06 MHz to 81.50 MHz under the same tool options and kept its transaction cycle count. A registered receiver descriptor boundary, banked matcher order state, and split arithmetic in the gateway are plausible next experiments. Each would change the cycle budget and needs to be measured against the same tests.

```sh
make verify
make route FREQ=100 SEEDS="1" ROUTE_JOBS=3
```

`build/characterization/result.json` records the device, tool versions and results. Each day folder keeps its Yosys log, mapped netlist, nextpnr log, and detailed report under `build/characterization/`. GitHub Actions attaches logs and summary reports to completed runs.
