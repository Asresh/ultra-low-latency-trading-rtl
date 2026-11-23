`timescale 1ns/1ps
`default_nettype none

// Use a Python-predicted two-sink schedule and compare every active pin.
module tb_message_rate_gate;
    logic clk = 0;
    logic rst = 1;
    logic s_valid = 0;
    logic s_ready;
    logic [1:0] s_op;
    logic [2:0] s_key;
    logic [11:0] s_tick;
    logic [11:0] s_amount;
    logic [11:0] s_rate;
    logic s_enable;
    logic [15:0] s_tag;
    logic [31:0] s_data;

    logic audit_valid;
    logic audit_ready;
    logic [1:0] audit_op;
    logic [2:0] audit_key;
    logic [15:0] audit_tag;
    logic [3:0] audit_status;
    logic [11:0] audit_balance;
    logic [15:0] audit_passed;
    logic [15:0] audit_limited;
    logic [15:0] audit_charged;
    logic [2:0] audit_saturated;

    logic out_valid;
    logic out_ready;
    logic [2:0] out_key;
    logic [15:0] out_tag;
    logic [31:0] out_data;

    integer source_fd;
    integer sink_fd;
    integer fields;
    integer cycle;
    integer d_rst, d_valid, d_audit_ready, d_out_ready, d_data;
    integer d_op, d_key, d_tick, d_amount, d_rate, d_enable, d_tag;
    string source;
    string sink;

    message_rate_gate dut (.*);

    // Sample both streams, omitting the unconstrained payload when valid is low.
    task automatic observe;
        $fdisplay(
            sink_fd,
            "%0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h",
            s_ready, audit_valid, out_valid,
            audit_valid ? audit_op : 2'b0,
            audit_valid ? audit_key : 3'b0,
            audit_valid ? audit_tag : 16'b0,
            audit_valid ? audit_status : 4'b0,
            audit_valid ? audit_balance : 12'b0,
            audit_valid ? audit_passed : 16'b0,
            audit_valid ? audit_limited : 16'b0,
            audit_valid ? audit_charged : 16'b0,
            audit_valid ? audit_saturated : 3'b0,
            out_valid ? out_key : 3'b0,
            out_valid ? out_tag : 16'b0,
            out_valid ? out_data : 32'b0
        );
    endtask

    // Put the next fixed-width fixture row on the pins, settle, then clock it.
    task automatic one_cycle;
        rst = 1'(d_rst);
        s_valid = 1'(d_valid);
        audit_ready = 1'(d_audit_ready);
        out_ready = 1'(d_out_ready);
        s_data = 32'(d_data);
        s_op = 2'(d_op);
        s_key = 3'(d_key);
        s_tick = 12'(d_tick);
        s_amount = 12'(d_amount);
        s_rate = 12'(d_rate);
        s_enable = 1'(d_enable);
        s_tag = 16'(d_tag);
        #4;
        observe();
        clk = 1;
        #1;
        clk = 0;
    endtask

    initial begin
        if (!$value$plusargs("source=%s", source) || !$value$plusargs("sink=%s", sink))
            $fatal(1, "provide +source and +sink gate paths");
        source_fd = $fopen(source, "r");
        sink_fd = $fopen(sink, "w");
        if (source_fd == 0 || sink_fd == 0)
            $fatal(1, "could not open gate schedule and capture");
        cycle = 0;
        #1;
        clk = 1;
        #1;
        clk = 0;
        while (!$feof(source_fd)) begin
            fields = $fscanf(
                source_fd,
                "%h %h %h %h %h %h %h %h %h %h %h %h",
                d_rst, d_valid, d_audit_ready, d_out_ready, d_data, d_op,
                d_key, d_tick, d_amount, d_rate, d_enable, d_tag
            );
            if (fields == 12) begin
                one_cycle();
                cycle = cycle + 1;
            end else if (fields > 0 || !$feof(source_fd))
                $fatal(1, "malformed gate vector %0d", cycle);
        end
        if (cycle == 0 || audit_valid || out_valid)
            $fatal(1, "gate vector schedule did not drain");
        $fclose(source_fd);
        $fclose(sink_fd);
        $display("sampled %0d dual-output gate cycles", cycle);
        $finish;
    end
endmodule

`default_nettype wire
