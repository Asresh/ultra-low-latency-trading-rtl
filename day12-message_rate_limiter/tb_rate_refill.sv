`timescale 1ns/1ps
`default_nettype none

// Exhaustively check balances, capacities, rates and modular distances on a
// small geometry, using ordinary integers rather than reduced-width logic.
module tb_rate_refill;
    logic [3:0] tick;
    logic [3:0] anchor;
    logic [3:0] balance;
    logic [3:0] capacity;
    logic [3:0] rate;
    logic chronological;
    logic [3:0] available;
    integer checked;

    rate_refill #(.TICK_W(4), .TOKEN_W(4)) dut (.*);

    // Compare a single laboriously enumerated state to an integer computation.
    task automatic verify(
        input integer start,
        input integer elapsed,
        input integer cap,
        input integer bal,
        input integer refill_rate
    );
        integer expected;
        logic expected_order;
        begin
            anchor = 4'(start);
            tick = 4'((start + elapsed) % 16);
            balance = 4'(bal);
            capacity = 4'(cap);
            rate = 4'(refill_rate);
            expected_order = elapsed < 8;
            expected = bal;
            if (expected_order) begin
                expected = bal + elapsed * refill_rate;
                if (expected > cap)
                    expected = cap;
            end
            #1;
            if (chronological !== expected_order || available !== 4'(expected))
                $fatal(1, "refill mismatch anchor=%0d elapsed=%0d cap=%0d bal=%0d rate=%0d",
                       start, elapsed, cap, bal, refill_rate);
            checked = checked + 1;
        end
    endtask

    initial begin
        checked = 0;
        for (integer start = 0; start < 16; start = start + 5)
            for (integer elapsed = 0; elapsed < 16; elapsed = elapsed + 1)
                for (integer cap = 0; cap < 16; cap = cap + 1)
                    for (integer bal = 0; bal <= cap; bal = bal + 1)
                        for (integer slope = 0; slope <= cap; slope = slope + 1)
                            verify(start, elapsed, cap, bal, slope);
        $display("refill exhaustively checked %0d combinations", checked);
        $finish;
    end
endmodule

`default_nettype wire
