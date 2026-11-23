# Research and design decisions

This review covers 30 papers and preprints across trading datapaths, hardware networking, priority queues, market simulation and FPGA implementation. Review depth varies. The design decisions below draw on accessible architecture and evaluation sections for the central hardware papers, and on abstracts for some adjacent work. The Nasdaq specification defines the decoder's actual wire fields.

## Decisions that made it into RTL

| Decision | Where it shows up | Tradeoff |
| --- | --- | --- |
| Keep transport integrity, message parsing and recovery policy separate | The receiver validates a whole datagram in one capture bank, reorders its messages by MoldUDP64 sequence, and requests missing ranges. The ITCH field decoder still accepts message-aligned beats. | A malformed trailing block drops its entire datagram. This costs buffering and latency, and allows the other line to supply a clean copy. A transport adapter transmits requests and selects the session epoch. |
| Size the memory for the hot operation | Each instrument bank uses a price occupancy bitmap and linked FIFO queues. A local command and event FIFO give different books independent progress. | A bounded tick range and dense IDs make lookup predictable. Added levels and orders raise mux and routing cost, and the shared arbiter limits external output to one event per clock. |
| Treat a financial state update as a transaction | The risk pipeline commits the slot and the ledger together. It tracks each admission, fill, cancel, reject and amendment with an owner key, generation and event sequence. | A single-issue ledger avoids RAW forwarding. This makes the order of state changes explicit and limits the initiation interval to four clocks. |
| Put integrity and latency under test | Exact decoded field scoreboards, price-time and quantity checks, and backpressure tests accompany the RTL. The performance report keeps accepted-beat, decision, fill, and routed clock timing distinct. | A component benchmark excludes MAC, PHY, host and network delay. |

## Trading and market data hardware

| # | Work | Design takeaway |
| --- | --- | --- |
| 1 | Leber, Geib and Litz, *High Frequency Trading Acceleration Using FPGAs* (2011). [Paper](https://hlitz.github.io/papers/hft_fpga.pdf) | An FPGA can decode market feed data without waiting for software. Their microcoded FAST decoder trades area and flexibility differently from a fixed ITCH decoder. |
| 2 | Litz et al., *DSL Programmable Engine for High Frequency Trading Acceleration*. [Paper](https://hlitz.github.io/papers/dsl_hft.pdf) | Moving the strategy and its datapath into hardware is distinct from decoding. Keep the parser output typed enough for multiple downstream consumers. |
| 3 | *FPGA Accelerated Low-Latency Market Data Feed Processing* (2009). [Paper](https://www.doc.ic.ac.uk/~wl/papers/09/hoti09dt.pdf) | Separate receipt, decoding and delivery when comparing latency. Streaming extraction avoids waiting for unrelated fields. |
| 4 | Denholm et al., *Application-Specific Customisation of Market Data* (2013). [Paper](https://epics.uni-paderborn.de/publications/2013_denholm_fpt.pdf) | A downstream workload can consume a compact view of a feed. Decode just the order lifecycle subset and document what happens to other message types. |
| 5 | Denholm et al., *Network-Level FPGA Acceleration of Low Latency Market Data Feed Arbitration* (2015). [Paper](https://www.doc.ic.ac.uk/~wl/papers/15/ieice14.pdf) | Redundant-line arbitration and loss detection have their own latency and recovery tradeoffs. Keep A/B arbitration outside the decoder contract and preserve sequence telemetry. |
| 6 | Boutros, Grady, Abbas and Chow, *Build Fast, Trade Fast* (2017). [Author manuscript](https://andrewboutros.github.io/assets/pdf/papers/004_reconfig2017.pdf) | Parsing, order-book handling and strategy can be evaluated as separate blocks. Their HLS system frames the partition while these projects explore explicit RTL. |
| 7 | Oliveira and Bonato, *A FAST Hardware Decoder Optimized for Template Features to Obtain Order Book Data in Low Latency* (2023). [Publisher abstract](https://link.springer.com/article/10.1007/s11265-023-01850-2) | Template specificity is a defensible optimization. Their B3 FAST results use a different message-size distribution, so throughput figures need protocol and traffic context. |
| 8 | Puranik et al., *Acceleration of Trading System Back End with FPGAs Using High-Level Synthesis Flow* (2023). [Paper](https://www.mdpi.com/2079-9292/12/3/520) | Its add, update and delete data paths illustrate how memory organization affects throughput. The matcher uses direct indices and separates its cycle counts by fill count. |
| 9 | *Algorithmic Trading: A Brief, Computational Finance Case Study on Data Centre FPGAs* (2016). [Preprint](https://arxiv.org/abs/1607.05069) | Their options workloads distinguish task from pipeline parallelism. The same distinction matters when reporting sustained beat rate versus order service time. |
| 10 | Fu et al., *FinHardBench* (2026). [Preprint](https://arxiv.org/abs/2608.00909) | Clock frequency, stage count and system latency are separate measurements. The benchmark's idealized pipeline model motivates explicitly testing stalls and fill-count variation here. |

## Packet processing and stateful pipelines

| # | Work | Design takeaway |
| --- | --- | --- |
| 11 | Wang et al., *P4FPGA* (2017). [Paper](https://www.cs.cornell.edu/~jnfoster/papers/p4fpga.pdf) | Modular parse and downstream service stages make it possible to test the adapter contract independently. |
| 12 | da Silva, Boyer and Langlois, *P4-Compatible High-Level Synthesis of Low Latency 100 Gb/s Streaming Packet Parsers in FPGAs* (2018). [Preprint](https://arxiv.org/abs/1711.06613) | Stream width, parser depth and timing are separate levers. A 64-bit input width by itself establishes a bits-per-clock ceiling only. |
| 13 | Attig and Brebner, *400 Gb/s Programmable Packet Parsing on a Single FPGA* (2011). [Paper](https://cgi.cse.unsw.edu.au/~cs4601/refs/papers/ancs11attig.pdf) | Wider windows and staged extraction can scale a parser. ITCH's fixed offsets allow narrower, message-specific extraction at this prototype scale. |
| 14 | Forencich et al., *Corundum: An Open-Source 100-Gbps NIC* (2020). [Paper](https://cseweb.ucsd.edu/~snoeren/papers/corundum-fccm20.pdf) | NIC data movement, queues and scheduler matter to real latency. The decoder's payload interface leaves that infrastructure outside its measurement. |
| 15 | Zilberman et al., *NetFPGA SUME* (2014). [Paper](https://www.cl.cam.ac.uk/~nz247/publications/zilberman2014sume.pdf) | A physical reference platform specifies MACs, buses and clocks. Report standalone core results separately from board bring-up. |
| 16 | *Design Principles for Packet Deparsers on FPGAs* (2021). [Preprint](https://arxiv.org/abs/2103.07750) | Output serialization has its own routing and throughput cost. The gateway returns a normalized decision that a separate serializer can consume. |
| 17 | Pontarelli et al., *FlowBlaze: Stateful Packet Processing in Hardware* (2019). [Paper](https://www.usenix.org/system/files/nsdi19-pontarelli.pdf) | Traffic can hit the same state key on consecutive cycles. The risk scoreboard therefore stresses consecutive events on the same account, instrument, side and slot. |
| 18 | Sivaraman et al., *Packet Transactions* (2016). [Paper](https://people.eecs.berkeley.edu/~akcheung/papers/sigcomm16.html) | An atomic transaction defines a precise boundary for state visible to the next item. The gate has a single ordered decision step and the matcher commits each event once. |
| 19 | Wang et al., *Isolation Mechanisms for High-Speed Packet-Processing Pipelines* (2022). [Paper](https://www.usenix.org/system/files/nsdi22-paper-wang_tao.pdf) | Runtime control-plane updates need defined visibility. Configuration drains pending risk operations and responses before replacing a policy row. |
| 20 | Bosshart et al., *Programming Protocol-Independent Packet Processors* (2014). [Preprint](https://arxiv.org/abs/1312.1719) | Distinguish parsing a protocol from making a policy decision on its fields. The gateway consumes a normalized order record. |

## Priority, simulation and benchmarking

| # | Work | Design takeaway |
| --- | --- | --- |
| 21 | Atre et al., *BBQ: A Fast and Scalable Integer Priority Queue for Hardware Packet Scheduling* (2024). [Paper](https://web.stanford.edu/class/cs244/papers/nsdi24-atre.pdf) | A hierarchy of occupancy bitmaps scales integer priority lookup. Here two small occupancy maps identify best prices and per-price queues preserve time priority. |
| 22 | Sivaraman et al., *Programmable Packet Scheduling at Line Rate* (2016). [Paper](https://yuba.stanford.edu/~nickm/papers/p44-sivaraman.pdf) | Priority ordering can be decomposed into ordering among buckets and FIFO service within a bucket. In a book, the occupied tick is the bucket and cancellations need arbitrary unlink. |
| 23 | Shrivastav, *Fast, Scalable, and Programmable Packet Scheduler in Hardware* (2019). [Paper](https://web.ics.purdue.edu/~vshriva/papers/pieo_2019.pdf) | More general eligibility policies need more structure. The book keeps the simpler price-time rule explicit. |
| 24 | Gould et al., *Limit Order Books* (2013). [Paper](https://www.math.ucla.edu/~mason/papers/gould-qf-final.pdf) | Defines the behavior of limit, cancel and marketable orders. Resting price, time priority at one level and partial fills merit separate checks. |
| 25 | Huang, Lehalle and Rosenbaum, *Simulating and Analyzing Order Book Data: The Queue-Reactive Model* (2015). [Preprint](https://arxiv.org/abs/1312.0563) | Update intensity can depend on the state of the book. Randomized requests are useful for correctness and should be labeled synthetic when benchmarking. |
| 26 | Byrd, Hybinette and Balch, *ABIDES: Towards High-Fidelity Market Simulation for AI Research* (2019). [Preprint](https://arxiv.org/abs/1904.12066) | A simulator can model agents, messages and links outside the matching core. Define a precise boundary between RTL event time and simulated network time. |
| 27 | Laughlin, Aguirre and Grundfest, *Information Transmission Between Financial Markets in Chicago and New York* (2014). [Preprint](https://arxiv.org/abs/1302.5966) | Physical link latency can dominate the full path. Avoid comparing a core compute result with a geographic round trip. |
| 28 | Gerig, *High-Frequency Trading Synchronizes Prices in Financial Markets* (2012). [Preprint](https://arxiv.org/abs/1211.1919) | Related instruments update under different market conditions. Per-instrument identifiers and risk state are a useful architectural boundary. |
| 29 | Zhang, Zohren and Roberts, *DeepLOB* (2019). [Preprint](https://arxiv.org/abs/1808.03668) | A model can consume several book levels over time. Typed events and a defined top-of-book output enable a separate strategy frontend. |
| 30 | Shah et al., *Yosys+nextpnr* (2019). [Preprint](https://arxiv.org/abs/1903.10407) | A reproducible open flow separates logic mapping from place and route. A mapped cell count is neither routed utilization nor measured on-board latency. |

## Protocol, policy and RTL references

- [Nasdaq TotalView-ITCH 5.0](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/NQTVITCHSpecification_5.0.pdf) specifies type bytes, exact lengths, field order and big-endian integer encoding.
- [SEC market access controls FAQ](https://www.sec.gov/rules-regulations/staff-guidance/trading-markets-frequently-asked-questions/divisionsmarketregfaq-0) and [FIA risk control recommendations](https://www.fia.org/sites/default/files/2020-04/Trading_Best_Pratices%20-%20published.pdf) supply context for limits and emergency stops. The gateway is an engineering prototype.
- [RTL Projects Everyday](https://github.com/Asresh/RTL-Projects-Everyday) informed the independent project layouts and Make-driven verification. Days 25–31 were the closest reference track.
- [Synthesizable RTL Style skill](https://github.com/zhangyi-zen/synthesizable-rtl-style/blob/main/skills/synthesizable-rtl-style/SKILL.md) and the [lowRISC Verilog Coding Style Guide](https://github.com/lowRISC/style-guides/blob/master/VerilogCodingStyle.md) were read as style references. The projects use explicit widths, synchronous state updates and separated simulation code.
