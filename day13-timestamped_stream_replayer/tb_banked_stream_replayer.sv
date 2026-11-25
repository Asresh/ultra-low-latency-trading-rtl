`timescale 1ns/1ps
`default_nettype none

module tb_banked_stream_replayer #(
    parameter integer DEPTH = 3,
    parameter integer RUN_W = 3
);
    localparam integer COUNT_W = $clog2(DEPTH+1);
    logic clk = 0, rst = 1, cfg_valid = 0, m_ready = 0, rsp_ready = 0;
    logic [2:0] cfg_op;
    logic cfg_bank;
    logic [RUN_W-1:0] cfg_run_id;
    logic [31:0] cfg_tick, cfg_gap;
    logic [63:0] cfg_data, cfg_release_tick;
    logic [7:0] cfg_keep;
    logic cfg_last;
    logic [15:0] cfg_tag, cfg_repeats;
    wire cfg_ready, rsp_valid, m_valid, m_last, active, paused, m_bank, run_started, done;
    wire [3:0] rsp_status;
    wire [2:0] rsp_op;
    wire rsp_bank, rsp_image_open, done_bank, done_stopped, done_truncated;
    wire [RUN_W-1:0] rsp_run_id, m_run_id, done_run_id;
    wire [1:0] rsp_bank_state;
    wire [COUNT_W-1:0] rsp_length;
    wire [63:0] m_data, wall_tick, run_start_tick, done_tick;
    wire [7:0] m_keep;
    wire [15:0] m_tag, m_pass;
    wire [47:0] m_due, m_offered, now_tick;
    wire [31:0] done_beats, done_packets, done_late, done_passes;
    wire [31:0] started_runs, retired_runs, cancelled_runs, blocked_release_cycles;

    always #5 clk = !clk;
    banked_stream_replayer #(.DEPTH(DEPTH), .RUN_W(RUN_W)) dut (.*);

    integer fd, read_count, index = 0;
    integer commands = 0, transfers = 0, starts = 0, retirements = 0, truncations = 0;
    integer offer_stalls = 0, reply_stalls = 0, handoffs = 0, staged_writes = 0;
    string fixture, wave, capture_path;
    integer capture = 0;
    logic [3:0] incoming_flags;
    logic [236+RUN_W:0] incoming_command;
    logic [6:0] expected_flags;
    logic [10+RUN_W+COUNT_W:0] expected_reply;
    logic [200:0] expected_word;
    logic [RUN_W:0] expected_identity;
    logic [175:0] expected_ticks;
    logic [194+RUN_W:0] expected_retirement;
    logic [127:0] expected_totals;

    initial begin
        if (!$value$plusargs("vectors=%s",fixture))
            $fatal(1,"configure +vectors=PATH");
        fd = $fopen(fixture,"r");
        if (!fd)
            $fatal(1,"could not read %s",fixture);
        if ($value$plusargs("waves=%s",wave)) begin
            $dumpfile(wave);
            $dumpvars(1,tb_banked_stream_replayer);
            $dumpvars(1,tb_banked_stream_replayer.dut);
        end
        if ($value$plusargs("capture=%s",capture_path)) begin
            capture = $fopen(capture_path,"w");
            if (!capture)
                $fatal(1,"could not open %s",capture_path);
            $fdisplay(capture,"wall,run,bank,data,keep,last,tag,due,offered,pass");
        end
        @(posedge clk);
        while (!$feof(fd)) begin
            @(negedge clk);
            read_count = $fscanf(fd,"%h %h %h %h %h %h %h %h %h\n",
                incoming_flags,incoming_command,expected_flags,expected_reply,
                expected_word,expected_identity,expected_ticks,expected_retirement,expected_totals);
            if (read_count != 9)
                $fatal(1,"invalid fixture row %0d with %0d fields",index,read_count);
            {rst,cfg_valid,rsp_ready,m_ready} = incoming_flags;
            {cfg_op,cfg_bank,cfg_run_id,cfg_tick,cfg_data,cfg_keep,cfg_last,cfg_tag,
             cfg_repeats,cfg_gap,cfg_release_tick} = incoming_command;
            #1;
            if ({cfg_ready,rsp_valid,m_valid,active,paused,run_started,done} !== expected_flags)
                $fatal(1,"pin flags mismatch at %0d expected %b observed %b", index, expected_flags,
                       {cfg_ready,rsp_valid,m_valid,active,paused,run_started,done});
            if (rsp_valid &&
                {rsp_status,rsp_op,rsp_bank,rsp_run_id,rsp_bank_state,rsp_length,rsp_image_open} !== expected_reply)
                $fatal(1,"response mismatch at %0d expected %h observed %h",index,expected_reply,
                       {rsp_status,rsp_op,rsp_bank,rsp_run_id,rsp_bank_state,rsp_length,rsp_image_open});
            if ({m_data,m_keep,m_last,m_tag,m_due,m_offered,m_pass} !== expected_word)
                $fatal(1,"stream mismatch at %0d expected %h observed %h",index,expected_word,
                       {m_data,m_keep,m_last,m_tag,m_due,m_offered,m_pass});
            if ({m_bank,m_run_id} !== expected_identity)
                $fatal(1,"owner mismatch at %0d",index);
            if ({now_tick,wall_tick,run_start_tick} !== expected_ticks)
                $fatal(1,"time base mismatch at %0d",index);
            if ({done_bank,done_run_id,done_stopped,done_truncated,done_tick,done_beats,
                 done_packets,done_late,done_passes} !== expected_retirement)
                $fatal(1,"retirement record mismatch at %0d expected %h observed %h",index,
                       expected_retirement,{done_bank,done_run_id,done_stopped,done_truncated,done_tick,
                                            done_beats,done_packets,done_late,done_passes});
            if ({started_runs,retired_runs,cancelled_runs,blocked_release_cycles} !== expected_totals)
                $fatal(1,"lifetime counter mismatch at %0d",index);
            commands += cfg_valid && cfg_ready;
            staged_writes += cfg_valid && cfg_ready && active && cfg_op == 0 && cfg_bank != m_bank;
            transfers += m_valid && m_ready && !rst;
            if (capture && m_valid && m_ready && !rst)
                $fdisplay(capture,"%0d,%0d,%0d,%016h,%02h,%0d,%0d,%0d,%0d,%0d",wall_tick,
                          m_run_id,m_bank,m_data,m_keep,m_last,m_tag,m_due,m_offered,m_pass);
            offer_stalls += m_valid && !m_ready && !rst;
            reply_stalls += rsp_valid && !rsp_ready && !rst;
            starts += run_started && !rst;
            retirements += done && !rst;
            truncations += done && done_truncated && !rst;
            handoffs += done && run_started && !rst;
            index++;
            @(posedge clk);
        end
        $fclose(fd);
        if (capture)
            $fclose(capture);
        if (!$test$plusargs("bundle") &&
            (commands < 12 || transfers < 3 || starts < 2 || retirements < 2 || truncations < 1 ||
             offer_stalls < 3 || reply_stalls < 3 || handoffs < 1 || staged_writes < 1))
            $fatal(1,"fixture failed to exercise required coverage");
        $display("PASS banked replay: depth=%0d id bits=%0d, cycles=%0d commands=%0d transfers=%0d starts=%0d retirements=%0d truncations=%0d handoffs=%0d staged writes=%0d offer/reply stalled=%0d/%0d",
                 DEPTH,RUN_W,index,commands,transfers,starts,retirements,truncations,handoffs,
                 staged_writes,offer_stalls,reply_stalls);
        $finish;
    end
endmodule

`default_nettype wire
