`timescale 1ns/1ps
`default_nettype none

module tb_mold_recovery_guard #(
    parameter integer SLOTS = 3,
    parameter integer PACKET_BYTES = 72,
    parameter integer MESSAGE_BYTES = 24,
    parameter integer BLOCKS = 4,
    parameter integer WINDOW = 7,
    parameter integer EPOCH_W = 4,
    parameter integer TOKEN_W = 3
);
    localparam integer KEY_W = $clog2(SLOTS+1);
    localparam integer ROUTE_W = KEY_W+EPOCH_W+TOKEN_W;
    localparam integer BEAT_W = 74+ROUTE_W;
    localparam integer AUDIT_W = 101+ROUTE_W+WINDOW;
    localparam integer REPLY_W = 202+ROUTE_W+WINDOW;
    localparam integer CMD_W = 195+ROUTE_W;

    logic clk = 0, rst = 1;
    logic cmd_valid = 0, rx_valid = 0, rsp_ready = 0, replay_ready = 0, audit_ready = 0;
    logic [1:0] cmd_op;
    logic [KEY_W-1:0] cmd_slot, rx_slot, stat_slot;
    logic [EPOCH_W-1:0] cmd_epoch, rx_epoch;
    logic [TOKEN_W-1:0] cmd_token, rx_token;
    logic cmd_enable, rx_first, rx_last;
    logic [159:0] cmd_request;
    logic [31:0] cmd_ttl;
    logic [63:0] rx_data;
    logic [7:0] rx_keep;
    logic [4:0] stat_index;

    wire cmd_ready, rx_ready, rsp_valid, replay_valid, audit_valid, stat_valid;
    wire [1:0] rsp_op;
    wire [KEY_W-1:0] rsp_slot, replay_slot, audit_slot;
    wire [EPOCH_W-1:0] rsp_epoch, replay_epoch, audit_epoch;
    wire [TOKEN_W-1:0] rsp_token, replay_token, audit_token;
    wire rsp_enabled, replay_first, replay_last, audit_completed;
    wire [3:0] rsp_status, audit_status;
    wire [2:0] rsp_state;
    wire [159:0] rsp_request;
    wire [WINDOW-1:0] rsp_coverage, audit_coverage;
    wire [31:0] rsp_ttl, stat_value, total_frames, rejected_frames, unknown_slots;
    wire [63:0] replay_data, audit_sequence;
    wire [7:0] replay_keep;
    wire [15:0] audit_count, audit_bytes;

    always #5 clk = !clk;
    mold_recovery_guard #(
        .SLOTS(SLOTS), .PACKET_BYTES(PACKET_BYTES), .MESSAGE_BYTES(MESSAGE_BYTES),
        .BLOCKS(BLOCKS), .WINDOW(WINDOW), .EPOCH_W(EPOCH_W), .TOKEN_W(TOKEN_W)
    ) dut (.*);

    logic [5:0] flags, expected_flags;
    logic [CMD_W-1:0] command;
    logic [BEAT_W-1:0] beat, expected_beat;
    logic [KEY_W+4:0] stats;
    logic [REPLY_W-1:0] expected_reply;
    logic [AUDIT_W-1:0] expected_audit;
    logic [127:0] expected_counts;
    wire [REPLY_W-1:0] current_reply = {
        rsp_op,rsp_status,rsp_slot,rsp_epoch,rsp_token,rsp_enabled,rsp_state,
        rsp_request,rsp_coverage,rsp_ttl
    };
    wire [BEAT_W-1:0] current_beat = {
        replay_data,replay_keep,replay_first,replay_last,replay_slot,replay_epoch,replay_token
    };
    wire [AUDIT_W-1:0] current_audit = {
        audit_status,audit_slot,audit_epoch,audit_token,audit_sequence,audit_count,audit_bytes,
        audit_coverage,audit_completed
    };
    logic held_replay = 0, held_audit = 0, held_reply = 0;
    logic [BEAT_W-1:0] prior_beat;
    logic [AUDIT_W-1:0] prior_audit;
    logic [REPLY_W-1:0] prior_reply;
    logic [14:0] verdicts = '0;
    logic [9:0] replies = '0;
    integer file, fields, index = 0;
    integer received = 0, admitted = 0, replayed = 0, completed = 0, audited = 0;
    integer stalled_replay = 0, stalled_audit = 0, stalled_reply = 0, fences = 0;
    string fixture, waveform;

    initial begin
        if (!$value$plusargs("vectors=%s",fixture))
            $fatal(1,"configure +vectors=PATH");
        file = $fopen(fixture,"r");
        if (!file)
            $fatal(1,"could not open %s",fixture);
        if ($value$plusargs("waves=%s",waveform)) begin
            $dumpfile(waveform);
            $dumpvars(1,tb_mold_recovery_guard);
            $dumpvars(1,tb_mold_recovery_guard.dut.leases);
        end

        @(posedge clk);
        while (!$feof(file)) begin
            @(negedge clk);
            fields = $fscanf(file, "%h %h %h %h %h %h %h %h %h\n",
                flags,command,beat,stats,expected_flags,expected_reply,
                expected_beat,expected_audit,expected_counts);
            if (fields != 9)
                $fatal(1,"bad vector row %0d, read %0d fields",index,fields);
            {rst,cmd_valid,rx_valid,rsp_ready,replay_ready,audit_ready} = flags;
            {cmd_op,cmd_slot,cmd_epoch,cmd_token,cmd_enable,cmd_request,cmd_ttl} = command;
            {rx_data,rx_keep,rx_first,rx_last,rx_slot,rx_epoch,rx_token} = beat;
            {stat_slot,stat_index} = stats;
            #1;
            if ({cmd_ready,rx_ready,rsp_valid,replay_valid,audit_valid,stat_valid} !== expected_flags)
                $fatal(1,"flags mismatch cycle %0d expected %b got %b",index,expected_flags,
                       {cmd_ready,rx_ready,rsp_valid,replay_valid,audit_valid,stat_valid});
            if (rsp_valid && current_reply !== expected_reply)
                $fatal(1,"reply mismatch row %0d expected %h got %h",index,expected_reply,current_reply);
            if (replay_valid && current_beat !== expected_beat)
                $fatal(1,"replay mismatch row %0d expected %h got %h",index,expected_beat,current_beat);
            if (audit_valid && current_audit !== expected_audit)
                $fatal(1,"audit mismatch row %0d expected %h got %h",index,expected_audit,current_audit);
            if ({stat_value,total_frames,rejected_frames,unknown_slots} !== expected_counts)
                $fatal(1,"statistics mismatch row %0d expected %h got %h",index,expected_counts,
                       {stat_value,total_frames,rejected_frames,unknown_slots});
            if (!rst) begin
                if (held_replay && (!replay_valid || prior_beat !== current_beat))
                    $fatal(1,"held replay changed");
                if (held_audit && (!audit_valid || prior_audit !== current_audit))
                    $fatal(1,"held audit changed");
                if (held_reply && (!rsp_valid || prior_reply !== current_reply))
                    $fatal(1,"held response changed");
            end
            held_replay = replay_valid && !replay_ready;
            held_audit = audit_valid && !audit_ready;
            held_reply = rsp_valid && !rsp_ready;
            prior_beat = current_beat;
            prior_audit = current_audit;
            prior_reply = current_reply;
            received += rx_valid && rx_ready && rx_last;
            stalled_replay += held_replay;
            stalled_audit += held_audit;
            stalled_reply += held_reply;
            fences += cmd_valid && dut.leases.replay_fence;
            replayed += replay_valid && replay_ready && replay_last;
            if (audit_valid && audit_ready) begin
                verdicts[audit_status] = 1;
                audited++;
                admitted += audit_status == 0;
                completed += audit_completed;
            end
            if (rsp_valid && rsp_ready)
                replies[rsp_status] = 1;
            index++;
            @(posedge clk);
        end
        $fclose(file);
        if (verdicts !== 15'h7fff || (TOKEN_W == 3 && replies !== 10'h3ff) ||
            completed < 1 || fences < 8 || stalled_replay < 8 ||
            stalled_audit < 8 || stalled_reply < 2)
            $fatal(1,"coverage failed verdicts=%h responses=%h",verdicts,replies);
        $display("PASS recovery guard: %0d clocks, %0d input frames, %0d audits, %0d admissions, %0d replayed, %0d complete, %0d fenced cycles, replay/audit/reply stalls %0d/%0d/%0d",
                 index,received,audited,admitted,replayed,completed,fences,stalled_replay,stalled_audit,stalled_reply);
        $finish;
    end
endmodule

`default_nettype wire
