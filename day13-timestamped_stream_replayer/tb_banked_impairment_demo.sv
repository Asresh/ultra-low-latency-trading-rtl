`timescale 1ns/1ps
`default_nettype none

module tb_banked_impairment_demo;
    localparam integer RUN_W = 5, RULES = 4, KEY_W = 3, REV_W = 4;
    localparam integer DEPTH = 16, QUEUE_DEPTH = 3, MAX_BEATS = 8;
    localparam integer RULE_W = 198+RUN_W;
    localparam integer AUDIT_W = 280+RUN_W+REV_W+KEY_W;
    localparam integer OCC_W = $clog2(QUEUE_DEPTH+1);
    localparam integer COUNT_W = $clog2(DEPTH+1);

    logic clk = 0, rst = 1;
    always #5 clk = !clk;
    logic replay_cfg_valid = 0, replay_cfg_bank;
    wire replay_cfg_ready, replay_rsp_valid;
    logic [2:0] replay_cfg_op;
    logic [31:0] replay_cfg_tick, replay_cfg_gap;
    logic [63:0] replay_cfg_data, replay_cfg_release;
    logic [7:0] replay_cfg_keep;
    logic replay_cfg_last;
    logic [15:0] replay_cfg_tag, replay_cfg_repeats;
    wire [3:0] replay_status;
    wire [COUNT_W-1:0] tape_length;
    wire [RUN_W-1:0] replay_rsp_run, done_run;
    wire [63:0] wall_tick;
    wire done, done_truncated, done_bank, source_active;

    wire s_valid, s_ready, s_last, s_bank;
    wire [63:0] s_data;
    wire [7:0] s_keep;
    wire [15:0] s_tag, s_pass;
    wire [47:0] s_due, s_offered;
    wire [RUN_W-1:0] s_run;
    logic source_open = 0;
    wire s_first = !source_open;

    banked_stream_replayer #(.DEPTH(DEPTH), .RUN_W(RUN_W)) source (
        .clk, .rst, .cfg_valid(replay_cfg_valid), .cfg_ready(replay_cfg_ready),
        .cfg_op(replay_cfg_op), .cfg_bank(replay_cfg_bank), .cfg_run_id(RUN_W'(0)),
        .cfg_tick(replay_cfg_tick), .cfg_data(replay_cfg_data), .cfg_keep(replay_cfg_keep),
        .cfg_last(replay_cfg_last), .cfg_tag(replay_cfg_tag), .cfg_repeats(replay_cfg_repeats),
        .cfg_gap(replay_cfg_gap), .cfg_release_tick(replay_cfg_release),
        .rsp_valid(replay_rsp_valid), .rsp_ready(1'b1), .rsp_status(replay_status), .rsp_op(),
        .rsp_bank(), .rsp_run_id(replay_rsp_run), .rsp_bank_state(), .rsp_length(tape_length), .rsp_image_open(),
        .m_valid(s_valid), .m_ready(s_ready), .m_data(s_data), .m_keep(s_keep), .m_last(s_last),
        .m_tag(s_tag), .m_due(s_due), .m_offered(s_offered), .m_pass(s_pass), .active(source_active), .paused(),
        .m_bank(s_bank), .m_run_id(s_run), .now_tick(), .wall_tick, .run_started(),
        .run_start_tick(), .done, .done_bank, .done_stopped(), .done_truncated, .done_run_id(done_run),
        .done_tick(), .done_beats(), .done_packets(), .done_late(), .done_passes(), .started_runs(),
        .retired_runs(), .cancelled_runs(), .blocked_release_cycles()
    );

    logic cfg_valid = 0;
    wire cfg_ready, rsp_valid;
    logic [1:0] cfg_op;
    logic [REV_W-1:0] cfg_expected_revision;
    logic [KEY_W-1:0] cfg_slot;
    logic [31:0] cfg_seed;
    logic [RULE_W-1:0] cfg_rule;
    wire [2:0] rsp_status;
    wire [REV_W-1:0] rsp_revision;

    wire m_valid, m_ready, m_last, m_first, m_copy, m_bank;
    wire [63:0] m_data, m_eligible;
    wire [7:0] m_keep;
    wire [15:0] m_tag, m_pass;
    wire [47:0] m_due, m_offered;
    wire [RUN_W-1:0] m_run;
    wire [REV_W-1:0] m_revision;
    wire [31:0] m_ordinal;
    wire [1:0] m_action;
    wire audit_valid;
    wire [AUDIT_W-1:0] audit_data;
    wire [63:0] audit_sequence, audit_cursor;
    wire [OCC_W-1:0] occupancy, peak_occupancy;
    wire [31:0] frames, drops, duplicates, corruptions, flip_misses, lost_audits, emitted;
    assign m_ready = wall_tick[2:0] < 5;

    replay_impairment #(
        .RULES(RULES), .QUEUE_DEPTH(QUEUE_DEPTH), .MAX_BEATS(MAX_BEATS), .RUN_W(RUN_W), .REV_W(REV_W)
    ) shaper (
        .clk, .rst, .wall_tick,
        .cfg_valid, .cfg_ready, .cfg_op, .cfg_expected_revision, .cfg_slot, .cfg_read_active(1'b0),
        .cfg_seed, .cfg_rule, .rsp_valid, .rsp_ready(1'b1), .rsp_op(), .rsp_status, .rsp_revision,
        .rsp_slot(), .rsp_seed(), .rsp_rule(), .s_valid, .s_ready, .s_first, .s_last,
        .s_data, .s_keep, .s_tag, .s_pass, .s_due, .s_offered, .s_bank, .s_run,
        .abort_valid(done && done_truncated), .abort_bank(done_bank), .abort_run(done_run),
        .m_valid, .m_ready, .m_first, .m_last, .m_copy, .m_data, .m_keep, .m_tag, .m_pass,
        .m_due, .m_offered, .m_bank, .m_run, .m_revision, .m_ordinal, .m_action, .m_eligible,
        .audit_valid, .audit_ready(1'b1), .audit_sequence, .audit_cursor, .audit_data,
        .occupancy, .peak_occupancy, .frames, .drops, .duplicates, .corruptions, .flip_misses,
        .malformed(), .oversizes(), .aborted(), .emitted, .lost_audits, .sink_stall_cycles()
    );

    string directory;
    integer src, snk, audit_file, meta, stimulus;
    integer retired = 0, held = 0;
    logic [318:0] preceding;
    logic stalled = 0;
    wire [318:0] offer = {
        m_first,m_last,m_copy,m_bank,m_data,m_keep,m_tag,m_pass,m_due,m_offered,
        m_run,m_revision,m_ordinal,m_action,m_eligible
    };
    localparam string CSV_FIELDS = "wall,bank,run,tag,pass,first,last,data,keep,due,offered,ordinal,copy,action,revision,eligible";
    always @(posedge clk) begin
        if (rst) begin
            source_open = 0;
            stalled = 0;
        end else begin
            if (stalled && (!m_valid || preceding !== offer))
                $fatal(1,"impaired egress changed while independently stalled");
            preceding = offer;
            stalled = m_valid && !m_ready;
            held += stalled;
            if (s_valid && s_ready) begin
                $fdisplay(src,"%0d,%0d,%0d,%0d,%0d,%0d,%0d,%016h,%02h,%0d,%0d,0,0,0,0,0",
                          wall_tick,s_bank,s_run,s_tag,s_pass,s_first,s_last,s_data,s_keep,s_due,s_offered);
                source_open <= !s_last;
            end
            if (done) begin
                if (done_truncated)
                    source_open <= 0;
                retired++;
            end
            if (m_valid && m_ready)
                $fdisplay(snk,"%0d,%0d,%0d,%0d,%0d,%0d,%0d,%016h,%02h,%0d,%0d,%0d,%0d,%0d,%0d,%0d",
                          wall_tick,m_bank,m_run,m_tag,m_pass,m_first,m_last,m_data,m_keep,m_due,m_offered,
                          m_ordinal,m_copy,m_action,m_revision,m_eligible);
            if (audit_valid)
                $fdisplay(audit_file,"%0d %h",audit_sequence,audit_data);
        end
    end

    task automatic transact_source(input logic [2:0] op, input logic bank);
        replay_cfg_op = op;
        replay_cfg_bank = bank;
        replay_cfg_valid = 1;
        do @(posedge clk); while (!replay_cfg_ready);
        @(negedge clk);
        replay_cfg_valid = 0;
        while (!replay_rsp_valid)
            @(negedge clk);
        if (replay_status != 0 || (op == 2 && replay_rsp_run != RUN_W'({1'b0,bank}+2'd1)))
            $fatal(1,"banked operation %0d on bank %0d failed with status %0d",op,bank,replay_status);
        @(negedge clk);
    endtask

    initial begin
        integer commands, fields, beats;
        string waves;
        if (!$value$plusargs("out=%s",directory))
            $fatal(1,"configure +out=DIR");
        if ($value$plusargs("waves=%s",waves)) begin
            $dumpfile(waves);
            $dumpvars(1,tb_banked_impairment_demo);
            $dumpvars(1,tb_banked_impairment_demo.shaper);
        end
        src = $fopen({directory,"/impairment-source.csv"},"w");
        snk = $fopen({directory,"/impairment-sink.csv"},"w");
        audit_file = $fopen({directory,"/impairment-audits.txt"},"w");
        meta = $fopen({directory,"/impairment-meta.txt"},"w");
        stimulus = $fopen({directory,"/commands.txt"},"r");
        if (!src || !snk || !audit_file || !meta || !stimulus)
            $fatal(1,"could not open one of the banked integration fixtures");
        $fdisplay(src,"%s",CSV_FIELDS);
        $fdisplay(snk,"%s",CSV_FIELDS);
        repeat (3) @(negedge clk);
        rst = 0;
        if ($fscanf(stimulus,"%d",commands) != 1 || commands < 2)
            $fatal(1,"invalid rule command count");
        for (integer index = 0; index < commands; index++) begin
            fields = $fscanf(stimulus,"%h %h %h %h %h",
                             cfg_op,cfg_expected_revision,cfg_slot,cfg_seed,cfg_rule);
            if (fields != 5)
                $fatal(1,"incomplete rule command");
            cfg_valid = 1;
            do @(posedge clk); while (!cfg_ready);
            @(negedge clk);
            cfg_valid = 0;
            if (!rsp_valid || rsp_status != 0 ||
                (cfg_op == 3 && rsp_revision != REV_W'(1)))
                $fatal(1,"compiled recipe did not install");
            @(negedge clk);
        end
        for (integer bank = 0; bank < 2; bank++) begin
            if ($fscanf(stimulus,"%d",beats) != 1 || beats < 1 || beats > DEPTH)
                $fatal(1,"invalid tape length");
            for (integer index = 0; index < beats; index++) begin
                fields = $fscanf(stimulus,"%h %h %h %h %h",replay_cfg_tick,replay_cfg_data,
                                 replay_cfg_keep,replay_cfg_last,replay_cfg_tag);
                if (fields != 5)
                    $fatal(1,"incomplete compiled tape");
                transact_source(0,1'(bank));
                if (tape_length != COUNT_W'(index+1))
                    $fatal(1,"banked tape reader did not accept its exact image");
            end
        end
        replay_cfg_repeats = 2;
        replay_cfg_gap = 2;
        replay_cfg_release = wall_tick + 5;
        transact_source(2,0);
        while (!source_active) @(negedge clk);
        replay_cfg_repeats = 1;
        replay_cfg_gap = 0;
        replay_cfg_release = wall_tick;
        transact_source(2,1);
        while (retired < 2 || occupancy || m_valid || audit_valid)
            @(negedge clk);
        repeat (4) @(negedge clk);
        if (source_open || lost_audits || frames != 14 || drops != 3 || duplicates != 3 ||
            corruptions != 2 || flip_misses != 2 || emitted != 14 || held < 10 ||
            peak_occupancy != OCC_W'(QUEUE_DEPTH))
            $fatal(1,"live banked impairment coverage or metrics disagree");
        $fdisplay(meta,"%0d %0d",audit_cursor,lost_audits);
        $fclose(stimulus);
        $fclose(src);
        $fclose(snk);
        $fclose(audit_file);
        $fclose(meta);
        $display("PASS live banked impairment: %0d source frames, %0d output frames, %0d stalled edges",
                 frames,emitted,held);
        $finish;
    end

    initial begin
        #40000;
        $fatal(1,"native banked impairment fixture timed out");
    end
endmodule

`default_nettype wire
