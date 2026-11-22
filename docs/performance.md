# Verification and performance

Measure cycle behavior with `make test`. Measure the three default core configurations with `make route`. Run `make synth` for a generic Yosys netlist and structural checks independent of the FPGA mapping.

## Simulation evidence

| Core | Test workload | Checks |
| --- | --- | --- |
| ITCH decoder | 1,544 expected completions per seed over seeds 7201 and 39119 | Every normalized output bit, frame errors and sequence disposition. Each run checks 90 consecutive input beats and 21 final-beat to next-edge transfers. |
| Matching engine | 2,531 commands and 2,697 events at 7 levels / 5 slots, 2,542 commands and 2,844 events at 16 / 16, and 209 commands and events at 1 / 1 | Independent price and arrival-time model, priority and quantity invariants, linked-list integrity, output stalls, cancellations and ID reuse. The default book run includes 564 fills. |
| Risk gateway | 1,482 decisions, 214 allowances, 1,268 rejections, 902 held-output cycles | Independent notional ledger, all eight rejection bits, same-instrument bursts, exact-width arithmetic, halt and reconfiguration. |
| Risk-to-matcher demo | 7 gateway decisions, 5 matcher events, 13 risk output stall cycles | Only approved requests enter the book, and the two makers at the same price trade in FIFO order. The scoreboard checks side exposure, rejected quantity, gross credit and the latched halt. |

## Routed core characterization

Device: Lattice ECP5 LFE5U-85F, CABGA381, speed grade 8. Tools: Yosys 0.71+77 (11d972c20), nextpnr 0.11.1-121 (15b77f5f), OSS CAD Suite 2026-11-22. The reference run uses a 100 MHz constraint, seed 1 and a single routing thread.

<!-- Timing table generated from nextpnr reports. -->
| Core | Target (MHz) | Routed Fmax (MHz) | Target met | Comb cells | FFs | 18×18 DSPs |
| --- | ---: | ---: | :---: | ---: | ---: | ---: |
| itch_decode | 100.00 | 150.94 | Yes | 2098 | 1651 | 0 |
| price_time_matcher | 100.00 | 82.19 | No | 1556 | 321 | 0 |
| pretrade_risk_gateway | 100.00 | 103.01 | Yes | 2361 | 1512 | 4 |

The out-of-context flow measures register paths and excludes board I/O timing, pin placement, global clock routing, MAC/PHY and network effects. Target frequency affects the optimization, and the route uses `--timing-allow-fail` so a miss remains visible in the report. The comb column reports `TRELLIS_COMB` sites, the FF column `TRELLIS_FF` sites, and the DSP column `MULT18X18D` instances.

The 64-bit decoder requires 156.25 MHz for a raw 10 Gb/s bus rate. A four-seed run at that constraint reached 150.94, 157.26, 154.85, and 148.63 MHz for seeds 1 through 4. Seed 2 met the clock constraint. Reproduce it with `make build/ecp5/itch_decode.timing.json FREQ=156.25 SEED=2`. It accepts padded frame tails, so useful ITCH payload rate also depends on message size. The matcher accepts at most one new single-event command every two clocks and reports one fill per clock during a sweep. The gateway can accept one order per clock while the sink is ready. These are RTL interface rates under the stated conditions. They do not include transport or system round-trip time.

```sh
make -j3 test
make -j3 synth
make -j3 route FREQ=100 SEED=1
```

See `build/ecp5/*.nextpnr.log` for the critical paths and `build/ecp5/*.timing.json` for the raw numbers. The individual project READMEs define what each input and output timestamp measures.
