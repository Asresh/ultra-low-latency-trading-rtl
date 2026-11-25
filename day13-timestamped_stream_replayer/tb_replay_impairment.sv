`timescale 1ns/1ps
`default_nettype none

module tb_replay_impairment #(
    parameter integer RULES = 4,
    parameter integer QUEUE_DEPTH = 3,
    parameter integer MAX_BEATS = 4,
    parameter integer RUN_W = 5,
    parameter integer REV_W = 3
);
    localparam integer KEY_W = $clog2(RULES+1);
    localparam integer RULE_W = 198+RUN_W;
    localparam integer AUDIT_W = 280+RUN_W+REV_W+KEY_W;
    localparam integer OCC_W = $clog2(QUEUE_DEPTH+1);
    localparam integer INPUT_W = 203+RUN_W;
    localparam integer OUTPUT_W = 302+RUN_W+REV_W;
    localparam integer CFG_W = 35+RULE_W+KEY_W+REV_W;
    localparam integer RSP_W = 37+RULE_W+KEY_W+REV_W;

    logic clk = 0, rst = 1;
    logic s_valid = 0, cfg_valid = 0, abort_valid = 0, m_ready = 0, rsp_ready = 0, audit_ready = 0;
    logic [63:0] wall_tick, s_data;
    logic [7:0] s_keep;
    logic s_first, s_last, s_bank, abort_bank;
    logic [15:0] s_tag, s_pass;
    logic [47:0] s_due, s_offered;
    logic [RUN_W-1:0] s_run, abort_run;
    logic [1:0] cfg_op;
    logic [REV_W-1:0] cfg_expected_revision;
    logic [KEY_W-1:0] cfg_slot;
    logic cfg_read_active;
    logic [31:0] cfg_seed;
    logic [RULE_W-1:0] cfg_rule;
    wire s_ready, m_valid, rsp_valid, audit_valid, cfg_ready;
    wire [1:0] rsp_op, m_action;
    wire [2:0] rsp_status;
    wire [REV_W-1:0] rsp_revision, m_revision;
    wire [KEY_W-1:0] rsp_slot;
    wire [31:0] rsp_seed;
    wire [RULE_W-1:0] rsp_rule;
    wire m_first, m_last, m_copy, m_bank;
    wire [63:0] m_data, m_eligible;
    wire [7:0] m_keep;
    wire [15:0] m_tag, m_pass;
    wire [47:0] m_due, m_offered;
    wire [RUN_W-1:0] m_run;
    wire [31:0] m_ordinal;
    wire [63:0] audit_sequence, audit_cursor;
    wire [AUDIT_W-1:0] audit_data;
    wire [OCC_W-1:0] occupancy, peak_occupancy;
    wire [31:0] frames, drops, duplicates, corruptions, flip_misses;
    wire [31:0] malformed, oversizes, aborted, emitted, lost_audits, sink_stall_cycles;
    wire [3:0] verdict = audit_data[212:209];

    always #5 clk = !clk;
    replay_impairment #(
        .RULES(RULES), .QUEUE_DEPTH(QUEUE_DEPTH), .MAX_BEATS(MAX_BEATS), .RUN_W(RUN_W), .REV_W(REV_W)
    ) dut (.*);

    wire [OUTPUT_W-1:0] output_fields = {
        m_data,m_keep,m_first,m_last,m_tag,m_pass,m_due,m_offered,m_bank,m_run,
        m_copy,m_revision,m_ordinal,m_action,m_eligible
    };
    wire [RSP_W-1:0] response_fields = {
        rsp_op,rsp_status,rsp_revision,rsp_slot,rsp_seed,rsp_rule
    };
    wire [351:0] count_fields = {
        frames,drops,duplicates,corruptions,flip_misses,malformed,oversizes,aborted,emitted,
        lost_audits,sink_stall_cycles
    };

    logic [6:0] flags;
    logic [INPUT_W-1:0] source_word;
    logic [RUN_W:0] abort_word;
    logic [CFG_W-1:0] control_word;
    logic [4:0] expected_flags;
    logic [RSP_W-1:0] expected_response;
    logic [OUTPUT_W-1:0] expected_output;
    logic [AUDIT_W+63:0] expected_audit;
    logic [95:0] expected_queue;
    logic [351:0] expected_counts;
    logic [7:0] verdicts = 0;
    logic [4:0] statuses = 0;
    integer fixture, fields, row = 0;
    integer inputs = 0, outs = 0, completed = 0, lost = 0;
    integer stalled_output = 0, stalled_reply = 0, stalled_audit = 0;
    integer full_drops = 0, second_copies = 0, kept_commits = 0;
    string path, wave_path;

    initial begin
        if (!$value$plusargs("vectors=%s",path))
            $fatal(1,"configure +vectors=PATH");
        fixture = $fopen(path,"r");
        if (!fixture)
            $fatal(1,"could not open %s",path);
        if ($value$plusargs("waves=%s",wave_path)) begin
            $dumpfile(wave_path);
            $dumpvars(1,tb_replay_impairment);
            $dumpvars(1,tb_replay_impairment.dut);
        end

        @(posedge clk);
        while (!$feof(fixture)) begin
            @(negedge clk);
            fields = $fscanf(fixture,"%h %h %h %h %h %h %h %h %h %h %h\n",
                flags,wall_tick,source_word,abort_word,control_word,expected_flags,expected_response,
                expected_output,expected_audit,expected_queue,expected_counts);
            if (fields != 11)
                $fatal(1,"invalid fixture row %0d with %0d fields",row,fields);
            {rst,s_valid,cfg_valid,abort_valid,m_ready,rsp_ready,audit_ready} = flags;
            {s_data,s_keep,s_first,s_last,s_tag,s_pass,s_due,s_offered,s_bank,s_run} = source_word;
            {abort_bank,abort_run} = abort_word;
            {cfg_op,cfg_expected_revision,cfg_slot,cfg_read_active,cfg_seed,cfg_rule} = control_word;
            #1;
            if ({s_ready,cfg_ready,rsp_valid,m_valid,audit_valid} !== expected_flags)
                $fatal(1,"handshake row %0d expected %b observed %b",row,expected_flags,
                       {s_ready,cfg_ready,rsp_valid,m_valid,audit_valid});
            if (rsp_valid && response_fields !== expected_response)
                $fatal(1,"configuration row %0d expected %h observed %h",row,expected_response,response_fields);
            if (output_fields !== expected_output)
                $fatal(1,"output row %0d expected %h observed %h",row,expected_output,output_fields);
            if (audit_valid && {audit_sequence,audit_data} !== expected_audit)
                $fatal(1,"audit row %0d expected %h observed %h",row,expected_audit,{audit_sequence,audit_data});
            if ({16'(occupancy),16'(peak_occupancy),audit_cursor} !== expected_queue ||
                count_fields !== expected_counts)
                $fatal(1,"counter row %0d expected %h/%h observed %h/%h",row,expected_queue,expected_counts,
                       {16'(occupancy),16'(peak_occupancy),audit_cursor},count_fields);
            if (!rst) begin
                inputs += s_valid && s_ready && s_last;
                outs += m_valid && m_ready && m_last;
                stalled_output += m_valid && !m_ready;
                stalled_reply += rsp_valid && !rsp_ready;
                stalled_audit += audit_valid && !audit_ready;
                full_drops += s_valid && s_ready && s_first && dut.policy_action == 1 &&
                              occupancy == OCC_W'(QUEUE_DEPTH);
                kept_commits += cfg_valid && cfg_ready && cfg_op == 3 && dut.open_packet;
                second_copies += m_valid && m_ready && m_last && m_copy;
                lost += dut.complete_capture && !dut.can_audit;
                if (audit_valid && audit_ready) begin
                    completed++;
                    verdicts[verdict] = 1;
                end
                if (rsp_valid && rsp_ready)
                    statuses[rsp_status] = 1;
            end
            row++;
            @(posedge clk);
        end
        $fclose(fixture);
        if (verdicts !== 8'hff || (REV_W == 3 && statuses !== 5'h1f) ||
            !full_drops || !kept_commits || !second_copies || !lost ||
            stalled_output < 5 || stalled_reply < 5 || stalled_audit < 3)
            $fatal(1,"coverage failed verdicts=%h statuses=%h stalls=%0d/%0d/%0d",
                   verdicts,statuses,stalled_output,stalled_reply,stalled_audit);
        $display("PASS replay impairment: %0d cycles, %0d input frames, %0d emitted copies, %0d copied twice, %0d audits, %0d lost, %0d/%0d/%0d output/reply/audit stalls",
                 row,inputs,outs,second_copies,completed,lost,stalled_output,stalled_reply,stalled_audit);
        $finish;
    end
endmodule

`default_nettype wire
