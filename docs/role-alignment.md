# How the portfolio maps to FPGA trading roles

Posting requirements were reviewed on November 23, 2026. Project evidence is linked from the root README.

| Firm and role | Role requirements | What to read or demonstrate |
| --- | --- | --- |
| Optiver FPGA Engineer | High-speed networking, low latency and high bandwidth design, timing and area constraints, self-checking simulations, and systems integration | Walk a MoldUDP64 frame through capture, length validation, reordering, recovery and ITCH decode. Show how ingress bandwidth, replay latency, and recovered-message ordering differ. Reproduce mapped area and timing. [Role](https://prod-www.optiver.com/join-us/jobs/technology/chicago/fpga-engineer/) |
| Citadel Securities FPGA Engineer | SystemVerilog or VHDL, hardware architecture, RTL simulation, synthesis, validation and integration for latency-sensitive trading | Demonstrate same-price priority under concurrent instrument traffic, event ordering under stalls, and round-robin behavior. Run the matcher scoreboard and explain the storage and arbiter paths from the RTL and timing report. [Role](https://www.citadelsecurities.com/careers/details/fpga-engineer/) |
| Jane Street Hardware Engineer, FPGA/ASIC | Hardware design from scratch, software-supported design and testing, FPGA or ASIC flows, and an interest in networking. The team uses Hardcaml. | Explain what the software reference model independently checks, trace a stalled transaction through the hardware, and inspect synthesis and route results. The portfolio implementation is SystemVerilog. [Role](https://www.janestreet.com/join-jane-street/position/8651405002/) |
| IMC Hardware Engineer | FPGA development for electronic trading, system design, testing, and close software collaboration | Describe the boundary between a transport session and FPGA state, and use the transaction demo to reconcile a partial execution using two sides' order handles. [Role](https://www.imc.com/us/careers/jobs/4987749101) |
| DRW FPGA Engineer (Algo) | FPGA designs for automated trading, electronic trading performance, and debugging in an engineering team | Present the exact transaction timeline and latency under backpressure, fault injection for stale input, and the measured routing bottleneck. [Role](https://www.drw.com/work-at-drw/listings/fpga-engineer-algo-3170998) |

## Design review prompts

1. Why must a malformed second message in a redundant datagram leave the book free to accept a good copy from the other line?
2. When a packet appears ahead of the expected sequence, what state proves a gap exists? How are wraparound and the bounded reorder window handled?
3. Which events from two instruments can reorder? What forces orders and output events to retain order within an instrument when one output is held?
4. Which commands change priority? What quantity is executed versus rested, expired, or reduced?
5. Why is a reservation valued at the limit price while a fill uses execution price? What prevents a duplicate execution or a fill for a reused order ID from changing risk?
6. Where does the latency measurement start and stop? Explain the difference between a cycle count, post-route clock frequency and a NIC-to-wire measurement.
7. Which data structures dominate routing and area? What would have to change for a deeper order book or for more concurrent data channels?
