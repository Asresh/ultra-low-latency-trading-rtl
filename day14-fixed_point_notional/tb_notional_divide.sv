`timescale 1ns/1ps
`default_nettype none

module tb_notional_divide #(parameter integer NUM_W = 80, parameter integer DIVISOR = 10000);
    localparam integer REM_W = $clog2(DIVISOR);
    logic clk = 0;
    always #5 clk = ~clk;
    logic rst = 1, s_valid = 0, m_ready = 0;
    logic [NUM_W-1:0] s_value = '0;
    wire s_ready, m_valid;
    wire [NUM_W-1:0] m_quotient;
    wire [REM_W-1:0] m_remainder;
    integer checked = 0, holds = 0;
    logic [31:0] state = 32'hd82bc121;

    notional_divide #(.NUM_W(NUM_W),.DIVISOR(DIVISOR)) dut (.*);

    // Independent deterministic source that decorrelates high and low numerator bits.
    function automatic logic [31:0] next_bits(input logic [31:0] prior);
        logic [31:0] x;
        begin
            x = prior ^ (prior << 13);
            x = x ^ (x >> 17);
            next_bits = x ^ (x << 5);
        end
    endfunction

    // Check quotient identities and result stability beyond the done cycle.
    task automatic trial(input logic [NUM_W-1:0] numerator, input integer wait_edges);
        logic [NUM_W-1:0] expected_q;
        logic [REM_W-1:0] expected_r;
        expected_q = numerator / NUM_W'(DIVISOR);
        expected_r = REM_W'(numerator % NUM_W'(DIVISOR));
        @(negedge clk);
        s_value = numerator;
        s_valid = 1;
        m_ready = 0;
        @(posedge clk);
        if (!s_ready || m_valid) $fatal(1,"Divider failed to accept a new request");
        @(negedge clk);
        s_valid = 0;
        s_value = ~numerator;
        for (integer digit = 0; digit < NUM_W; digit++) begin
            if (s_ready || m_valid) $fatal(1,"Divider exposed early state at digit %0d",digit);
            @(negedge clk);
        end
        repeat (wait_edges) begin
            if (!m_valid || s_ready || m_quotient !== expected_q || m_remainder !== expected_r)
                $fatal(1,"%0d/%0d held quotient wrong",NUM_W,DIVISOR);
            holds++;
            @(negedge clk);
        end
        m_ready = 1;
        @(posedge clk);
        if (!m_valid || m_quotient !== expected_q || m_remainder !== expected_r)
            $fatal(1,"%0d/%0d numerator %h: q=%h/%h r=%h/%h",
                   NUM_W,DIVISOR,numerator,m_quotient,expected_q,m_remainder,expected_r);
        checked++;
    endtask

    initial begin
        logic [NUM_W-1:0] word;
        repeat (3) @(negedge clk);
        rst = 0;
        if (NUM_W <= 10) begin
            for (integer n = 0; n < (1 << NUM_W); n++)
                trial(NUM_W'(n), n % 3);
        end else begin
            trial('0, 5);
            trial('1, 5);
            trial(NUM_W'(DIVISOR - 1), 3);
            trial(NUM_W'(DIVISOR), 3);
            trial(NUM_W'(DIVISOR + 1), 0);
            for (integer i = 0; i < NUM_W; i++)
                trial(NUM_W'(1) << i, i % 4);
            for (integer i = 0; i < 180; i++) begin
                state = next_bits(state);
                word = NUM_W'(state);
                state = next_bits(state);
                word = (word << 32) | NUM_W'(state);
                state = next_bits(state);
                word = (word << 32) | NUM_W'(state);
                trial(word,i % 4);
            end
        end
        if (holds < 200) $fatal(1,"Insufficient ready/valid hold coverage");
        $display("PASS divider width=%0d divisor=%0d for %0d numerators and %0d held edges",
                 NUM_W,DIVISOR,checked,holds);
        $finish;
    end
    initial begin #1000000; $fatal(1,"Divider watchdog"); end
endmodule

`default_nettype wire
