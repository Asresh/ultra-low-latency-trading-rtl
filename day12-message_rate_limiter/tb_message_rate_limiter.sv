`timescale 1ns/1ps
`default_nettype none

// Read an immutable request schedule and dump pre-edge pin observations.
// The independent Python driver checks every visible field after simulation.
module tb_message_rate_limiter #(
    parameter integer KEYS = 5,
    parameter integer KEY_W = (KEYS < 2) ? 1 : $clog2(KEYS),
    parameter integer TICK_W = 12,
    parameter integer TOKEN_W = 12,
    parameter integer COUNT_W = 16,
    parameter integer TAG_W = 16
);
    logic clk = 0;
    logic rst = 1;
    logic s_valid = 0;
    logic s_ready;
    logic [1:0] s_op = '0;
    logic [KEY_W-1:0] s_key = '0;
    logic [TICK_W-1:0] s_tick = '0;
    logic [TOKEN_W-1:0] s_amount = '0;
    logic [TOKEN_W-1:0] s_rate = '0;
    logic s_enable = 0;
    logic [TAG_W-1:0] s_tag = '0;

    logic m_valid;
    logic m_ready = 0;
    logic [1:0] m_op;
    logic [KEY_W-1:0] m_key;
    logic [TAG_W-1:0] m_tag;
    logic [3:0] m_status;
    logic m_enabled;
    logic [TICK_W-1:0] m_anchor;
    logic [TOKEN_W-1:0] m_capacity;
    logic [TOKEN_W-1:0] m_rate;
    logic [TOKEN_W-1:0] m_before;
    logic [TOKEN_W-1:0] m_balance;
    logic [COUNT_W-1:0] m_passed;
    logic [COUNT_W-1:0] m_limited;
    logic [COUNT_W-1:0] m_charged;
    logic [2:0] m_saturated;

    integer vector_file;
    integer capture_file;
    integer row;
    integer fields;
    integer accepted;
    integer transferred;
    integer blocked;
    integer control_reset;
    integer control_valid;
    integer control_ready;
    integer argument_op;
    integer argument_key;
    integer argument_tick;
    integer argument_amount;
    integer argument_rate;
    integer argument_enable;
    integer argument_tag;
    string vector_path;
    string capture_path;

`ifdef RATE_NETLIST
    message_rate_limiter dut (.*);
`else
    message_rate_limiter #(
        .KEYS(KEYS),
        .KEY_W(KEY_W),
        .TICK_W(TICK_W),
        .TOKEN_W(TOKEN_W),
        .COUNT_W(COUNT_W),
        .TAG_W(TAG_W)
    ) dut (.*);
`endif

    // Sample pins only after combinational ready and refill nets have settled.
    task automatic capture_cycle;
        if ($isunknown({s_ready, m_valid}))
            $fatal(1, "unknown control signal at vector row %0d", row);
        if (m_valid === 1'b1) begin
            if ($isunknown(m_status) || $isunknown(m_balance) || $isunknown(m_tag))
                $fatal(1, "unknown response field at vector row %0d", row);
            $fdisplay(
                capture_file,
                "%0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h",
                s_ready, m_valid, m_op, m_key, m_tag, m_status, m_enabled,
                m_anchor, m_capacity, m_rate, m_before, m_balance,
                m_passed, m_limited, m_charged, m_saturated
            );
        end else begin
            $fdisplay(capture_file, "%0h 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0", s_ready);
        end
    endtask

    // Apply an input row with a stable setup interval, then advance one edge.
    task automatic drive_cycle;
        rst = 1'(control_reset);
        s_valid = 1'(control_valid);
        m_ready = 1'(control_ready);
        s_op = 2'(argument_op);
        s_key = KEY_W'(argument_key);
        s_tick = TICK_W'(argument_tick);
        s_amount = TOKEN_W'(argument_amount);
        s_rate = TOKEN_W'(argument_rate);
        s_enable = 1'(argument_enable);
        s_tag = TAG_W'(argument_tag);
        #4;
        capture_cycle();
        if (!rst) begin
            if (s_ready && s_valid)
                accepted = accepted + 1;
            if (m_ready && m_valid)
                transferred = transferred + 1;
            if (m_valid && !m_ready)
                blocked = blocked + 1;
        end
        clk = 1;
        #1;
        clk = 0;
    endtask

    initial begin
        if (!$value$plusargs("vectors=%s", vector_path))
            $fatal(1, "missing +vectors=<input.tsv>");
        if (!$value$plusargs("capture=%s", capture_path))
            $fatal(1, "missing +capture=<output.tsv>");
        vector_file = $fopen(vector_path, "r");
        if (vector_file == 0)
            $fatal(1, "cannot open %s", vector_path);
        capture_file = $fopen(capture_path, "w");
        if (capture_file == 0)
            $fatal(1, "cannot open %s", capture_path);
        row = 0;
        accepted = 0;
        transferred = 0;
        blocked = 0;
        // Prime the synchronous reset before the first sampled row.
        #1;
        clk = 1;
        #1;
        clk = 0;
        while (!$feof(vector_file)) begin
            fields = $fscanf(
                vector_file, "%h %h %h %h %h %h %h %h %h %h",
                control_reset, control_valid, control_ready, argument_op,
                argument_key, argument_tick, argument_amount, argument_rate, argument_enable,
                argument_tag
            );
            if (fields == 10) begin
                row = row + 1;
                drive_cycle();
            end else if (!$feof(vector_file) || fields > 0) begin
                $fatal(1, "malformed vector row %0d (%0d fields)", row + 1, fields);
            end
        end
        if (row == 0)
            $fatal(1, "schedule must contain at least one row");
        if (m_valid)
            $fatal(1, "schedule finished with a response still pending");
        $fclose(vector_file);
        $fclose(capture_file);
        $display("cycles=%0d requests=%0d transfers=%0d output_stalls=%0d",
                 row, accepted, transferred, blocked);
        $finish;
    end
endmodule

`default_nettype wire
