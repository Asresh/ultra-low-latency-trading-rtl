Build one new project in my `ultra-low-latency-trading-rtl` repository:

Day 09 · FPGA Multi-Venue Smart Order Router

Implement it from scratch in synthesizable SystemVerilog. Match the layout, verification depth, and documentation of Days 01–08. Read those projects first, especially the risk gateway, market-state engine, and OUCH session. Day 09 should demonstrate a distinct problem: deciding how to split a parent order across venues and keeping that decision consistent as quotes, credits, and execution reports change.

Research current public FPGA engineering job descriptions at trading firms and primary sources relevant to the design. Cite the sources in the README and explain which implementation or verification decisions they informed.

## Design

- Accept normalized venue quotes and parent orders for a bounded set of instruments. Use synthetic feeds and venue simulators.
- Rank venues by executable price after per-venue fees. Define deterministic tie-breaking, quote freshness, available size, and how venue eligibility changes. Check the parent limit against the actual child-order price.
- Split each parent order into child orders subject to visible liquidity, venue credits, and parent quantity. Reserve quantity and credit before publishing a child. Define what happens when output is stalled and the selected quote changes.
- Track partial fills, rejects, cancels, and late reports. Make duplicate reports idempotent. Use generation or sequence fencing so delayed messages cannot modify a reused slot.
- Add configurable venue disable and a kill switch. Define how both affect new children, stalled outputs, live children, and outstanding reservations.
- Support a four-venue default and a smaller formal configuration. Document interface contracts, arithmetic widths, rounding, overflow behavior, and storage limits.

## Verification

- Write an independent Python reference model and a self-checking RTL scoreboard. Compare child orders, execution updates, and reservation state transaction by transaction.
- Run deterministic corner cases and seeded randomized tests with backpressure. Include equal-price ties, stale quotes, partial fills followed by cancel, duplicate reports, reports for reused slots, credit exhaustion, and kill-switch races.
- Prove key invariants on a reduced configuration. Parent fill quantity never exceeds the original order, credits never underflow, and stale reports cannot change current state. Add covers for partial-fill/cancel races and venue failover.
- Run simulation, lint, synthesis, and formal checks. Measure unloaded decision latency and sustained throughput separately. Attempt ECP5 place and route at 100 MHz and report the achieved clock, resource use, and critical path, including any timing miss.

## Deliverables

- Put the project in `day09-multivenue_order_router/` with RTL, testbenches, the model, a Makefile, and a standalone README.
- Deliver more than 5,000 substantive handwritten source lines across RTL, verification, and tooling. Report those counts separately. Exclude comments, blank lines, documentation, and generated fixtures.
- Use color-coded Mermaid diagrams for the datapath, child-order lifecycle, and a fill/cancel race. Include a worked example with prices, fees, quantities, credit before and after, and the expected child orders.
- Add Day 09 to the root README, source inventory, and CI. Run its checks and the existing repository-wide verification before calling the project complete.
- Finish with the actual test results, exact source counts, measured performance, and any remaining limitations. Distinguish simulation cycle counts, routed clock estimates, and board measurements.

Build and verify the project. Continue through implementation, debugging, documentation, and integration instead of stopping at a design proposal.
