# Verification, timing, and source inventory

These measurements cover the audited Day 01–03 designs. The earlier release used different cores and test conditions. [Original prototype measurements](https://github.com/Asresh/ultra-low-latency-trading-rtl/blob/80c7c41209814901a715ce0ee029ee4868b742cf/docs/performance.md)

## Verification and measured cycles

| Design | Workload and check | Measurement |
| --- | --- | --- |
| Market data receiver | Two fault regression seeds compare 628 whole 588-bit events each and verify recovery requests, status, stalls and malformed packet atomicity. The new recovery case checks the last unambiguously forward sequence and its exclusive watermark. The default-size test checks 84 more events. | A one-message, 41-byte datagram transferred a decoded event 15 cycles after its last beat in the small stress configuration, with six simultaneous accepted beats on both A/B ports. |
| Banked matcher | Four differential runs compare 17,055 commands and 18,026 events across three geometries. An independent order and priority model checks every command and event. Formal proves FIFO sequence and arbiter safety and fairness for the documented three-entry or three-lane instances. | Unloaded first event in 3 cycles. Balanced three-bank query traffic transferred 1.0000 event/cycle over the active output window versus 0.5007 event/cycle for a single-bank burst. |
| Risk gateway | Two parameter profiles check 7,440 decisions against an independent reservation and execution ledger. A synthesized-netlist run checks 220 additional transactions. The host reader independently validates CRC and sequence for every serialized response. Focused tests cover maxima, event and generation exhaustion, and audit policy under backpressure. | An unstalled request accepted at edge n commits and raises a reply after n+3, with first reply transfer at n+4. The single-issue ledger accepts at most one command every four clocks. |
| Cross-core lifecycle | Two account/side ledgers and a banked matcher. One venue fill becomes a maker and a taker execution update. The test verifies reject isolation, execution price versus limit price, cancel release, and stale generation after reuse. | 9 risk responses, 4 venue events, and explicit holds on both response streams. |

## Source size

The count includes SystemVerilog RTL and testbenches, formal harnesses, and Python models and host tools. It excludes comments, docstrings, blank lines, Makefiles, Markdown, and generated artifacts. `make inventory` enforces 2,000 source lines for each of Days 01–03. `python3 scripts/source_inventory.py --compare` additionally compares same-name source against the original release in a clone with history.

| Project | RTL | Verification / tools | Total | Added or changed since baseline |
| --- | ---: | ---: | ---: | ---: |
| day01-redundant_market_data | 1074 | 1179 | 2253 | 1985 |
| day02-banked_matching_engine | 613 | 1663 | 2276 | 2234 |
| day03-order_risk_gateway | 590 | 1749 | 2339 | 2339 |

## Routed clock and area

The common characterization uses the OSS CAD Suite 2026-11-22, Yosys 0.71+77 with Slang elaboration, nextpnr-ecp5, LFE5U-85F/CABGA381 speed grade 8, seed 1 and a single route thread. It maps each default core separately. The gateway numbers cover the ledger core; its optional audit tap and serializer are independently synthesized and exercised in simulation.

| Core | Seed | Target MHz | Routed MHz | Met | Comb sites | FFs | DSPs | EBRs |
| --- | ---: | ---: | ---: | :---: | ---: | ---: | ---: | ---: |
| redundant_market_data | 1 | 100.00 | 46.48 | No | 14125 | 7014 | 0 | 20 |
| banked_matching_engine | 1 | 100.00 | 73.91 | No | 10172 | 2973 | 0 | 0 |
| hft_order_risk_gateway | 1 | 100.00 | 80.46 | No | 4320 | 2897 | 4 | 0 |

The 100 MHz common characterization target is a tool constraint. Output reports retain the measured clock even if a core misses the target. Clock estimates exclude device I/O, global clock routing, MAC/PHY and board paths. One event per clock in a cycle simulation does not establish link throughput.

The receiver's routing cost includes capture-bank selection and the reorder control carrying 64-bit sequence state. The matching path includes the best-price selection and per-order data feeding the event path. The gateway's implementation cost includes wide ledger arithmetic and decision logic. The measured gateway cycle counts above include the terminal-event priority fix.

```sh
make verify
make route FREQ=100 SEEDS="1" ROUTE_JOBS=3
```

`build/characterization/result.json` records the device, tool versions and results. Each day folder keeps its Yosys log, mapped netlist, nextpnr log, and detailed report under `build/characterization/`. GitHub Actions attaches logs and summary reports to completed runs.
