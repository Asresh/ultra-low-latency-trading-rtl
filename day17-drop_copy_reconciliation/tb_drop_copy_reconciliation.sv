`timescale 1ns/1ps
`default_nettype none
`include "reconcile_defs.svh"

module tb_drop_copy_reconciliation #(
    parameter integer SETS = 4,
    parameter integer WAIT_CYCLES = 24,
    parameter integer HISTORY_CYCLES = 40,
    localparam integer COUNT_W = $clog2(2 * SETS + 1)
);
    logic clk = 0;
    always #5 clk = !clk;

    logic rst = 1;
    logic s_primary_valid = 0, s_copy_valid = 0, s_epoch_valid = 0, m_ready = 0;
    logic [`DC_KEY_W-1:0] s_primary_key = 0, s_copy_key = 0;
    logic [`DC_PAYLOAD_W-1:0] s_primary_payload = 0, s_copy_payload = 0;
    logic [15:0] s_new_epoch = 0;

    wire s_primary_ready, s_copy_ready, s_epoch_ready, m_valid;
    wire [3:0] m_status, m_diff;
    wire [1:0] m_source;
    wire [`DC_KEY_W-1:0] m_key;
    wire [`DC_PAYLOAD_W-1:0] m_expected, m_observed;
    wire [31:0] m_age;
    wire [COUNT_W-1:0] m_flushed, pending_count;
    wire [15:0] active_epoch;
    wire [31:0] tick, observed_count, matched_count, conflict_count, duplicate_count;
    wire [31:0] missing_count, late_count, full_count, rejected_count, flushed_count;

    drop_copy_reconciliation #(
        .SETS(SETS), .WAIT_CYCLES(WAIT_CYCLES), .HISTORY_CYCLES(HISTORY_CYCLES),
        .RESET_TICK(32'hffff_fff0)
    ) dut (.*);

    localparam integer EVENT_W = `DC_KEY_W + `DC_PAYLOAD_W;
    localparam integer REPORT_W = 4 + 2 + `DC_KEY_W + 2 * `DC_PAYLOAD_W + 4 + 32 + COUNT_W;
    wire [REPORT_W-1:0] report =
        {m_status,m_source,m_key,m_expected,m_observed,m_diff,m_age,m_flushed};
    logic primary_stall = 0, copy_stall = 0, epoch_stall = 0, out_stall = 0;
    logic [EVENT_W-1:0] held_primary, held_copy;
    logic [REPORT_W-1:0] held_report;
    logic [15:0] held_epoch;

    string vector_dir, wave_path;
    integer inputs_file, observed_file, samples, columns;

    // Check that each stalled endpoint preserves valid and the entire payload.
    task automatic check_holds(input integer cycle);
        if (rst) begin
            primary_stall = 0;
            copy_stall = 0;
            epoch_stall = 0;
            out_stall = 0;
        end else begin
            if (primary_stall && (!s_primary_valid ||
                {s_primary_key,s_primary_payload} !== held_primary))
                $fatal(1,"primary valid/data changed under stall at cycle %0d", cycle);
            if (copy_stall && (!s_copy_valid || {s_copy_key,s_copy_payload} !== held_copy))
                $fatal(1,"copy valid/data changed under stall at cycle %0d", cycle);
            if (epoch_stall && (!s_epoch_valid || s_new_epoch !== held_epoch))
                $fatal(1,"epoch valid/data changed under stall at cycle %0d", cycle);
            if (out_stall && (!m_valid || report !== held_report))
                $fatal(1,"report valid/data changed under stall at cycle %0d", cycle);
            primary_stall = s_primary_valid && !s_primary_ready;
            copy_stall = s_copy_valid && !s_copy_ready;
            epoch_stall = s_epoch_valid && !s_epoch_ready;
            out_stall = m_valid && !m_ready;
            held_primary = {s_primary_key,s_primary_payload};
            held_copy = {s_copy_key,s_copy_payload};
            held_epoch = s_new_epoch;
            held_report = report;
        end
    endtask

    // Write one pre-edge pin sample for the external scoreboard.
    task automatic write_sample;
        $fdisplay(observed_file,
            "%h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h",
            s_primary_ready,s_copy_ready,s_epoch_ready,m_valid,
            m_valid ? m_status : 4'b0,m_valid ? m_source : 2'b0,
            m_valid ? m_key : {`DC_KEY_W{1'b0}},
            m_valid ? m_expected : {`DC_PAYLOAD_W{1'b0}},
            m_valid ? m_observed : {`DC_PAYLOAD_W{1'b0}},
            m_valid ? m_diff : 4'b0,m_valid ? m_age : 32'b0,
            m_valid ? m_flushed : {COUNT_W{1'b0}},
            active_epoch,pending_count,tick,observed_count,matched_count,conflict_count,
            duplicate_count,missing_count,late_count,full_count,rejected_count,flushed_count);
    endtask

    initial begin : run
        if (!$value$plusargs("vectors=%s", vector_dir))
            $fatal(1,"missing +vectors argument");
        if ($value$plusargs("waves=%s", wave_path)) begin
            $dumpfile(wave_path);
            $dumpvars(1, tb_drop_copy_reconciliation);
        end
        inputs_file = $fopen({vector_dir,"/inputs.hex"}, "r");
        observed_file = $fopen({vector_dir,"/observed.hex"}, "w");
        if (!inputs_file || !observed_file) $fatal(1,"could not open vector files");
        if ($fscanf(inputs_file,"%d",samples) != 1 || samples < 1)
            $fatal(1,"invalid sample count");
        $fdisplay(observed_file,"%0d",samples);
        for (integer cycle = 0; cycle < samples; cycle++) begin
            @(negedge clk);
            columns = $fscanf(inputs_file,"%h %h %h %h %h %h %h %h %h %h",
                rst,s_primary_valid,s_primary_key,s_primary_payload,
                s_copy_valid,s_copy_key,s_copy_payload,s_epoch_valid,s_new_epoch,m_ready);
            if (columns != 10) $fatal(1,"malformed stimulus row %0d",cycle);
            #1;
            check_holds(cycle);
            if (pending_count > COUNT_W'(2 * SETS))
                $fatal(1,"pending count exceeded physical capacity at cycle %0d",cycle);
            if (s_primary_ready && s_copy_ready)
                $fatal(1,"two source handshakes in one cycle at cycle %0d",cycle);
            write_sample();
        end
        @(negedge clk);
        $fclose(inputs_file);
        $fclose(observed_file);
        $display("Captured %0d checked cycles at %0d sets",samples,SETS);
        $finish;
    end
endmodule

`default_nettype wire
