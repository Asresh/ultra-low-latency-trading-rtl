`timescale 1ns/1ps

module tb_latency_timebase;
    logic clk = 1'b0;
    always #4 clk = !clk;
    logic reset = 1'b0;
    logic pps_async = 1'b0;
    logic rate_valid = 1'b0;
    wire rate_ready;
    logic [31:0] rate_ns = '0;
    logic [31:0] rate_fraction = '0;
    wire [63:0] ticks, gray_ticks, nanoseconds;
    wire [31:0] subnanoseconds;
    wire saturated, invalid_rate;
    wire pps_valid;
    logic pps_ready = 1'b0;
    wire [63:0] pps_ticks, pps_nanoseconds;
    wire [31:0] pps_missed;
    latency_timebase #(.INITIAL_NS(3), .INITIAL_FRACTION(32'hc0000000)) dut (.*);

    logic [95:0] expected_time = '0;
    logic [63:0] expected_period = {32'd3, 32'hc0000000};
    logic [63:0] expected_ticks = '0;
    logic [63:0] prior_gray = '0;
    integer observed = 0, updates = 0;
    integer pps_total = 0;
    logic previous_hold;
    logic [127:0] held_pps;
    logic watch_live = 0;
    logic expected_error = 0;

    always @(posedge clk) begin
        if (reset) begin
            expected_time = '0;
            expected_ticks = '0;
            prior_gray = '0;
            previous_hold = 1'b0;
            expected_period = {32'd3, 32'hc0000000};
            expected_error = 1'b0;
        end else if (watch_live) begin
            if ({nanoseconds, subnanoseconds} !== expected_time || ticks !== expected_ticks ||
                gray_ticks !== (expected_ticks ^ (expected_ticks >> 1)) || saturated)
                $fatal(1, "Timebase mismatch at tick %0d", expected_ticks);
            if (ticks != 0 && !$onehot(gray_ticks ^ prior_gray))
                $fatal(1, "Gray publication advanced multiple bits");
            prior_gray = gray_ticks;
            expected_time = expected_time + 96'(expected_period);
            expected_ticks = expected_ticks + 1;
            observed++;
            if (invalid_rate !== expected_error)
                $fatal(1, "Rate error flag diverged at %0d", ticks);
            if (rate_valid && rate_ready) begin
                updates++;
                if ({rate_ns, rate_fraction} != 0)
                    expected_period = {rate_ns, rate_fraction};
                else
                    expected_error = 1'b1;
            end
            if (previous_hold && (!pps_valid || {pps_ticks, pps_nanoseconds} !== held_pps))
                $fatal(1, "PPS tuple changed while downstream was blocked");
            previous_hold = pps_valid && !pps_ready;
            if (previous_hold)
                held_pps = {pps_ticks, pps_nanoseconds};
            if (pps_valid && pps_ready)
                pps_total++;
        end
    end

    task automatic set_period(input logic [31:0] integer_value, input logic [31:0] fraction);
        begin
            @(negedge clk);
            rate_ns = integer_value;
            rate_fraction = fraction;
            rate_valid = 1;
            do @(posedge clk); while (!rate_ready);
            @(negedge clk);
            rate_valid = 0;
        end
    endtask

    task automatic pps_signal(input integer phase);
        begin
            @(negedge clk);
            #(phase);
            pps_async = 1;
            repeat (6) @(negedge clk);
            pps_async = 0;
            repeat (5) @(negedge clk);
        end
    endtask

    initial begin
        #1 reset = 1;
        repeat (9) @(negedge clk);
        reset = 0;
        watch_live = 1;
        repeat (5) @(negedge clk);
        set_period(0, 32'h4000_0000);
        repeat (5) @(negedge clk);
        set_period(0, 0);
        repeat (2) @(negedge clk);
        if (!invalid_rate) $fatal(1, "Zero rate did not set sticky error");
        set_period(2, 32'hffff_ffff);
        pps_signal(3);
        if (!pps_valid || pps_ticks == 0 || pps_nanoseconds > nanoseconds)
            $fatal(1, "PPS stamp not delivered");
        pps_signal(1);
        pps_signal(2);
        if (!pps_valid || pps_missed != 2) $fatal(1, "PPS overrun tally incorrect");
        repeat (5) @(negedge clk);
        pps_ready = 1;
        @(negedge clk);
        if (pps_valid) $fatal(1, "PPS did not retire");
        pps_signal(2);
        if (pps_total != 2) $fatal(1, "PPS capture did not resume");
        for (integer frame = 0; frame < 50; frame++) begin
            set_period(32'(frame % 9), 32'h78fc345c ^ (32'(frame) * 19215261));
            repeat (3 + frame % 7) @(negedge clk);
        end
        reset = 1;
        repeat (5) @(negedge clk);
        reset = 0;
        repeat (63) @(negedge clk);
        if (invalid_rate || pps_missed != 0 || pps_valid)
            $fatal(1, "Reset left status or PPS state");
        if (observed < 500 || updates != 53)
            $fatal(1, "Coverage counters incomplete");
        $display("PASS timebase: %0d calibrated tick updates, %0d period writes and %0d PPS transfers",
                 observed, updates, pps_total);
        $finish;
    end
    initial begin #100000; $fatal(1,"timebase timeout"); end
endmodule
