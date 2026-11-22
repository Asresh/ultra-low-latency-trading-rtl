# 03 / Pre-trade risk gateway

A synthesizable SystemVerilog gateway that checks normalized orders against per-instrument limits before granting a tagged authorization. It accepts one request per clock when the decision stream is ready. The three register stages cover policy capture, full-width notional calculation, and a credit reservation made in input order. Orders for consecutive cycles can target the same instrument and side.

## Architecture

| Register | Work before the register |
| --- | --- |
| Capture | Validate the instrument index and snapshot its quantity, price, and per-order notional policy. |
| Calculate | Multiply unsigned price by unsigned quantity and evaluate the quantity and price checks in parallel. |
| Decide | Compare the full-width notional with the order cap and with the side's current available credit. Apply the halt, record the decision, and reserve credit on an allow. |

With space in the pipeline, an order transferred at edge `n` is captured at `n`, has a registered product at `n+1`, and presents `m_valid` and decision data immediately after `n+2`. The first output handshake can occur at `n+3`. Each stage can hold a single item, so three requests can remain in flight. Downstream backpressure propagates when all three stages are full. A held decision stays stable until its handshake, including across a halt request.

For each instrument and side, the gate stores session gross notional and remaining credit. Comparing the product directly with credit lets the add to gross and subtraction from credit run in parallel with the authorization check. Each accepted product fits the credit remaining, which also guarantees that the gross addition fits. When the decision enters the output register, the reservation is final for this session. A reject leaves the ledger unchanged. Stalling or later transferring the output neither charges it again nor releases credit.

## Interface

All inputs and outputs are in the `clk` domain. A channel transfers on a rising edge with both `valid` and `ready` high. A producer holds valid and its payload stable until that edge. Interpret decision fields when `m_valid` is high. Unused instrument encodings are out of range. Configure `NUM_INSTRUMENTS`, `PRICE_W`, `QTY_W`, and `TAG_W` as positive integers. The default is four instruments, 32 price bits, 24 quantity bits, and 16 tag bits. Instrument index width is `max(1, ceil(log2(NUM_INSTRUMENTS)))`. All notional ports have `PRICE_W + QTY_W` bits.

| Signals | Meaning |
| --- | --- |
| `s_valid`, `s_ready`, `s_tag`, `s_instrument`, `s_sell`, `s_price`, `s_qty` | Order input. Side `0` means buy and side `1` means sell. Use integer price units and integer quantity units. The tag is echoed verbatim. |
| `m_valid`, `m_ready`, `m_tag`, `m_instrument`, `m_sell`, `m_price`, `m_qty` | Ordered decision stream and echoed original order. Gate forwarding on a transfer with `m_allow` asserted. |
| `m_allow`, `m_reject`, `m_notional`, `m_gross_after` | Authorization, rejection mask, exact product, and this instrument and side's gross after the decision. Rejected requests return their current gross. An out-of-range instrument returns zero gross. |
| `cfg_valid`, `cfg_ready`, `cfg_instrument`, `cfg_enable` | Atomic policy write. An asserted `cfg_valid` stops new intake and waits for all old orders and decisions to drain. The old policy governs every in-flight order. |
| `cfg_min_price`, `cfg_max_price`, `cfg_max_qty`, `cfg_max_order_notional`, `cfg_max_buy_gross`, `cfg_max_sell_gross` | Inclusive thresholds. Gross and order limits use the same units as `price × qty`. A zero side cap blocks that side. |
| `halt_req`, `halted` | An asserted synchronous `halt_req` blocks decisions on that edge and latches the halt until reset. Previously committed decisions remain stable. |
| `rst` | Synchronous, active-high session reset. Clears pending work, halt, and ledger, and disarms all instruments. Ready/valid handshakes are suppressed while asserted. |

Reset starts with every instrument disabled. A valid write enables an instrument if `cfg_enable` is high, `0 < min_price <= max_price`, and both quantity and order notional maxima are positive. A malformed or disabled write disarms that instrument. Writes with an out-of-range index have no effect. Each reconfiguration retains consumed gross and sets that side's credit to `max(new_cap - consumed_gross, 0)`. This also applies when a disabled instrument is enabled again.

## Rejection mask

`m_allow` is high exactly when `m_reject` is zero. Applicable failures are reported together. An out-of-range or disabled instrument skips policy comparisons and can still report zero quantity or halt.

| Bit | Failure |
| ---: | --- |
| 0 | Instrument index is out of range. |
| 1 | Instrument is in range and disabled or unconfigured. |
| 2 | Quantity is zero. |
| 3 | Quantity exceeds its cap. |
| 4 | Price is outside the inclusive collar. |
| 5 | Order notional exceeds the single-order cap. |
| 6 | Order notional exceeds remaining session credit for this instrument and side. |
| 7 | The emergency halt is asserted or latched. |

## Run

```sh
make test
make synth
make timing
```

Simulation requires Icarus Verilog with SystemVerilog enabled by the Makefile. The testbench checks empty-pipeline latency, continuous intake, single-cycle halt, stalled output stability, overflow boundaries, reconfiguration, reset, and all rejection bits. It compares each decision with an independent integer ledger under a fixed seed. `make synth` uses Yosys and records a technology-independent structural check and cell summary in `build/synth.log`. Set `YOSYS` to a compatible executable if needed.

`make timing` maps the default configuration to the Lattice ECP5 LFE5U-85F, CABGA381, speed grade 8, then runs register-to-register place and route at a 100 MHz target with seed 1. It requires Yosys with `synth_ecp5` and `nextpnr-ecp5`. See `build/risk_ecp5_report.json` and `build/nextpnr-ecp5.log` for the routed results.

| Reference run | Result |
| --- | ---: |
| Post-route maximum clock estimate | 103.01 MHz |
| ECP5 combinational cells | 2,361 |
| ECP5 flip-flops | 1,512 |
| ECP5 18×18 multiplier blocks | 4 |

This is an out-of-context run with Yosys 0.71+77 and nextpnr-ecp5 0.11.1-121. The critical path runs from the captured order through DSP partial products and their carry chains into the notional register. The run excludes board I/O delays, integration routing, and hardware measurements. At the 100 MHz target, an unstalled decision appears 20 ns after the input sampling edge, and its first transfer edge is 30 ns after input transfer.

## Scope and timing trade-offs

The cap models cumulative authorization against a gross session budget. An independent policy system would be needed to reconcile executions, cancels, reservations, and risk across gateways. Treat reset as the boundary of an externally reconciled session. Its flush can discard authorization responses awaiting delivery. To stop forwarding already committed responses when an emergency halt arrives, the order sender must also enforce its own halt.

The default design uses small register tables with combinational reads. Increasing the number of instruments increases the selection muxes and state. Banking the ledger or moving it into RAM would introduce new read latency and require forwarding or scoreboarding for repeated instrument and side accesses. The latency above is in clocks. FPGA frequency and DSP mapping depend on the device, toolchain, constraints, and placement.
