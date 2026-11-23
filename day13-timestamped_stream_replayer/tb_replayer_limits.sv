`timescale 1ns/1ps
`default_nettype none
`include "replay_codes.svh"

// Directed near-limit injection makes a 48-bit overflow condition testable in seconds.
module tb_replayer_limits;
    localparam integer DEPTH = 3;
    localparam integer LENGTH_W = $clog2(DEPTH+1);
    localparam logic [47:0] MAX_TICK = 48'hffffffffffff;
    localparam logic [31:0] MAX_COUNT = 32'hffffffff;
    logic clk = 0;
    always #5 clk = !clk;

    logic rst = 1;
    logic cfg_valid = 0;
    logic [2:0] cfg_op = 0;
    logic [31:0] cfg_tick = 0, cfg_gap = 0;
    logic [63:0] cfg_data = 0;
    logic [7:0] cfg_keep = 0;
    logic cfg_last = 0;
    logic [15:0] cfg_tag = 0, cfg_repeats = 0;
    logic rsp_ready = 1;
    logic m_ready = 0;

    wire cfg_ready, rsp_valid, m_valid, m_last, active, paused, done;
    wire [3:0] rsp_status;
    wire [LENGTH_W-1:0] rsp_length;
    wire [63:0] m_data;
    wire [7:0] m_keep;
    wire [15:0] m_tag, m_pass;
    wire [47:0] m_due, m_offered, now_tick;
    wire [31:0] emitted_beats, emitted_packets, late_beats, completed_passes;

    timestamped_stream_replayer #(.DEPTH(DEPTH)) dut (.*);

    // Send a fully specified command and check that it accepted exactly once.
    task automatic configure(
        input logic [2:0] op,
        input logic [31:0] tick,
        input logic [63:0] payload,
        input logic [7:0] keep,
        input logic last,
        input logic [15:0] tag,
        input logic [15:0] repeats,
        input logic [31:0] gap,
        input logic [3:0] expected_status,
        input logic [LENGTH_W-1:0] expected_length
    );
        begin
            @(negedge clk);
            cfg_valid = 1;
            cfg_op = op;
            cfg_tick = tick;
            cfg_data = payload;
            cfg_keep = keep;
            cfg_last = last;
            cfg_tag = tag;
            cfg_repeats = repeats;
            cfg_gap = gap;
            @(posedge clk);
            if (!cfg_ready)
                $fatal(1,"configuration should be ready in limits fixture");
            #1;
            if (!rsp_valid || rsp_status != expected_status || rsp_length != expected_length)
                $fatal(1,"configuration result mismatch for %0d",op);
            cfg_valid = 0;
        end
    endtask

    // Compare the complete valid offer, then release one transfer edge.
    task automatic take(
        input logic [47:0] due,
        input logic [47:0] offered,
        input logic [15:0] pass_number,
        input logic [63:0] payload,
        input logic [7:0] keep,
        input logic last
    );
        begin
            @(negedge clk);
            m_ready = 1;
            @(posedge clk);
            if (!m_valid || m_data != payload || m_keep != keep || m_last != last ||
                m_tag != 16'hbcad || m_due != due || m_offered != offered || m_pass != pass_number)
                $fatal(1,"unexpected %0d/%0h offer: valid=%b due=%0h offered=%0h pass=%0d data=%0h",
                    pass_number,payload,m_valid,m_due,m_offered,m_pass,m_data);
            #1;
            m_ready = 0;
        end
    endtask

    // Verify every saturated public counter together to catch partial reset bugs.
    task automatic check_stats(
        input logic [31:0] beats,
        input logic [31:0] packets,
        input logic [31:0] late,
        input logic [31:0] passes
    );
        if ({beats,packets,late,passes} !==
            {emitted_beats,emitted_packets,late_beats,completed_passes})
            $fatal(1,"counter mismatch: %0h/%0h/%0h/%0h",
                emitted_beats,emitted_packets,late_beats,completed_passes);
    endtask

    // Inject a starting value through the simulation hierarchy without changing RTL.
    task automatic approach_tick_limit;
        begin
            @(negedge clk);
            dut.now_tick = MAX_TICK - 48'd2;
            dut.emitted_beats = MAX_COUNT - 1'b1;
            dut.emitted_packets = MAX_COUNT - 1'b1;
            dut.late_beats = MAX_COUNT - 1'b1;
            dut.completed_passes = MAX_COUNT - 1'b1;
        end
    endtask

    initial begin
        @(posedge clk);
        @(negedge clk);
        rst = 0;
        configure(`RP_APPEND,0,64'h0123456789abcdef,8'hff,0,16'hbcad,0,0,`RP_OK,1);
        configure(`RP_APPEND,1,64'hdeadbeefcafebaa1,8'h01,1,16'hbcad,0,0,`RP_OK,2);
        configure(`RP_START,0,0,0,0,0,2,100,`RP_OK,2);
        approach_tick_limit;
        @(posedge clk);
        #1;
        if (now_tick != MAX_TICK-1'b1 || !active)
            $fatal(1,"time injection didn't advance on an active edge");
        take(0, MAX_TICK-48'd2, 1, 64'h0123456789abcdef,8'hff,0);
        check_stats(MAX_COUNT, MAX_COUNT-1'b1, MAX_COUNT, MAX_COUNT-1'b1);
        take(1, MAX_TICK-1'b1, 1, 64'h00000000000000a1,8'h01,1);
        check_stats(MAX_COUNT, MAX_COUNT, MAX_COUNT, MAX_COUNT);
        @(posedge clk);
        #1;
        if (now_tick != MAX_TICK || !active)
            $fatal(1,"repeat clock failed to stay saturated");
        take(MAX_TICK,MAX_TICK,2,64'h0123456789abcdef,8'hff,0);
        take(MAX_TICK,MAX_TICK,2,64'h00000000000000a1,8'h01,1);
        check_stats(MAX_COUNT, MAX_COUNT, MAX_COUNT, MAX_COUNT);
        if (!done || active || m_valid)
            $fatal(1,"terminal status missing despite last accepted beat");
        @(negedge clk);
        rst = 1;
        @(posedge clk);
        #1;
        check_stats(0,0,0,0);
        if (now_tick != 0 || done || m_valid || rsp_valid)
            $fatal(1,"reset left stale state after saturation");
        $display("PASS replay 48-bit time and 32-bit statistic bounds");
        $finish;
    end
endmodule

`default_nettype wire
