# Design target: 64-bit interface at the nominal 10 Gb/s lane clock.
# Integration supplies pin, I/O delay, and any board-level constraints.
create_clock -name stream_clk -period 6.400 [get_ports clk]
