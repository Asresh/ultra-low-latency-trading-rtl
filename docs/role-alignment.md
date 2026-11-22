# Trading hardware role alignment

The projects support discussion of hardware boundaries, throughput, register paths and verification results. Job descriptions were reviewed on November 22, 2026.

| Role | Requirement described by the firm | Evidence in this repository |
| --- | --- | --- |
| Citadel Securities FPGA Engineer | Hardware architecture, SystemVerilog or VHDL, simulation, integration, synthesis and static analysis for latency-sensitive trading | Explain where ITCH sequence handling belongs, how the gateway updates financial state under stall and how matching state advances per event. Reproduce simulation and mapping with Make. [Citadel Securities](https://www.citadelsecurities.com/careers/details/fpga-engineer/) |
| Optiver FPGA Engineer | High-speed networking, FPGA or ASIC design, synthesizing with constraints, self-checking testbenches and collaboration with software | Use the ITCH byte-lane and frame contract, discuss backpressure against upstream burst rate and compare the matching and risk critical paths. [Optiver](https://prod-www.optiver.com/join-us/jobs/technology/chicago/fpga-engineer/) |
| Jane Street Hardware Engineer, FPGA/ASIC | Ground-up hardware design, software-supported testing and integration, networking, and vendor tool experience. The team uses Hardcaml. | Discuss reference models, deterministic interfaces and portability across RTL tools. The portfolio uses SystemVerilog and the open ECP5 flow. [Jane Street](https://www.janestreet.com/join-jane-street/position/8651405002/) |

## Design review prompts

1. What is the exact cycle from the last accepted ITCH beat to output validity? How does a full result queue affect that measurement?
2. How would a larger price window change the priority encoder, memory ports and achievable frequency?
3. What happens when two consecutive orders share an instrument and side, including when the decision output stalls?
4. Which risk events are necessary to reconcile outstanding exposure, and how would an operator stop new orders while accounting for already committed work?
5. Which latency measurements include frame serialization and networking, and which begin at a ready/valid boundary?
