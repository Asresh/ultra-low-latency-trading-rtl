`timescale 1ns/1ps
`default_nettype none

module tb_latency_pps_discipline #(
    parameter integer REV_W = 3,
    parameter integer AUDIT_DEPTH = 3
);
    localparam integer OCC_W = $clog2(AUDIT_DEPTH+1);
    logic clk = 0, rst = 1;
    logic cfg_valid = 0, sample_valid = 0, rsp_ready = 0, audit_ready = 0, rate_ready = 0;
    logic [63:0] now_tick, sample_tick;
    logic [1:0] cfg_op;
    logic [REV_W-1:0] cfg_expected_rev;
    logic cfg_enable;
    logic [63:0] cfg_reference_ns, cfg_min_ticks, cfg_max_ticks, cfg_timeout;
    logic [63:0] cfg_max_age, cfg_jitter, cfg_min_period, cfg_max_period, cfg_max_step;
    logic [7:0] cfg_lock_windows;
    wire cfg_ready, sample_ready, rsp_valid, audit_valid, rate_valid;
    wire [1:0] rsp_op, rsp_mode, mode, interval_count;
    wire [2:0] rsp_status;
    wire [REV_W-1:0] rsp_rev, audit_rev;
    wire [63:0] rsp_period, rate_period, anchor_tick, active_period;
    wire [63:0] audit_sequence, audit_cursor;
    wire [325:0] audit_data;
    wire [3:0] audit_code = audit_data[325:322];
    wire [OCC_W-1:0] audit_occupancy;
    wire anchor_valid;
    wire [7:0] stable_windows;
    wire [31:0] sampled, invalid_samples, usable_intervals, applied_rates;
    wire [31:0] rejected_periods, holdovers, lost_audits;
    always #5 clk = !clk;

    latency_pps_discipline #(.REV_W(REV_W), .AUDIT_DEPTH(AUDIT_DEPTH)) dut (.*);

    logic [5:0] flags;
    logic [586+REV_W:0] config_word;
    logic [4:0] expected_flags;
    logic [70+REV_W:0] expected_reply;
    logic [389+REV_W:0] expected_audit;
    logic [63:0] expected_rate;
    logic [79:0] expected_queue;
    logic [140:0] expected_live;
    logic [223:0] expected_counts;
    logic [13:0] audit_codes = 0;
    logic [3:0] response_codes = 0;
    logic [3:0] modes = 0;
    integer file, fields, row = 0, updates = 0, observed = 0, audits = 0;
    integer held_rate = 0, held_audit = 0, held_reply = 0, losses = 0;
    string fixture, wave;
    wire [140:0] live_word = {
        mode,anchor_valid,anchor_tick,active_period,interval_count,stable_windows
    };
    wire [223:0] counts = {
        sampled,invalid_samples,usable_intervals,applied_rates,rejected_periods,holdovers,lost_audits
    };

    initial begin
        if (!$value$plusargs("vectors=%s",fixture))
            $fatal(1,"configure +vectors=PATH");
        file = $fopen(fixture,"r");
        if (!file)
            $fatal(1,"could not open %s",fixture);
        if ($value$plusargs("waves=%s",wave)) begin
            $dumpfile(wave);
            $dumpvars(1,tb_latency_pps_discipline);
            $dumpvars(1,tb_latency_pps_discipline.dut);
        end

        @(posedge clk);
        while (!$feof(file)) begin
            @(negedge clk);
            fields = $fscanf(file,"%h %h %h %h %h %h %h %h %h %h %h\n",
                flags,config_word,now_tick,sample_tick,expected_flags,expected_reply,expected_audit,
                expected_rate,expected_queue,expected_live,expected_counts);
            if (fields != 11)
                $fatal(1,"invalid fixture row %0d, %0d fields",row,fields);
            {rst,cfg_valid,sample_valid,rsp_ready,audit_ready,rate_ready} = flags;
            {cfg_op,cfg_expected_rev,cfg_enable,cfg_reference_ns,cfg_min_ticks,cfg_max_ticks,
             cfg_timeout,cfg_max_age,cfg_jitter,cfg_min_period,cfg_max_period,cfg_max_step,
             cfg_lock_windows} = config_word;
            #1;
            if ({cfg_ready,sample_ready,rsp_valid,audit_valid,rate_valid} !== expected_flags)
                $fatal(1,"flags differ row %0d expected %b got %b",row,expected_flags,
                       {cfg_ready,sample_ready,rsp_valid,audit_valid,rate_valid});
            if (rsp_valid && {rsp_op,rsp_status,rsp_rev,rsp_period,rsp_mode} !== expected_reply)
                $fatal(1,"response differs row %0d expected %h got %h",row,expected_reply,
                       {rsp_op,rsp_status,rsp_rev,rsp_period,rsp_mode});
            if ({audit_sequence,audit_rev,audit_data} !== expected_audit)
                $fatal(1,"audit differs row %0d expected %h got %h",row,expected_audit,{audit_sequence,audit_rev,audit_data});
            if (rate_period !== expected_rate || {audit_cursor,16'(audit_occupancy)} !== expected_queue)
                $fatal(1,"rate or audit queue occupancy differs at row %0d",row);
            if (live_word !== expected_live || counts !== expected_counts)
                $fatal(1,"state differs row %0d expected %h/%h received %h/%h",row,
                       expected_live,expected_counts,live_word,counts);

            if (!rst) begin
                modes[mode] = 1;
                observed += sample_valid && sample_ready;
                held_rate += rate_valid && !rate_ready;
                held_reply += rsp_valid && !rsp_ready;
                held_audit += audit_valid && !audit_ready;
                updates += rate_valid && rate_ready;
                losses += dut.trace && !dut.audit_available;
                if (audit_valid && audit_ready) begin
                    audit_codes[audit_code] = 1;
                    audits++;
                end
                if (rsp_valid && rsp_ready)
                    response_codes[rsp_status] = 1;
            end
            row++;
            @(posedge clk);
        end
        $fclose(file);
        if (audit_codes !== 14'h3fff || modes !== 4'hf ||
            (REV_W == 3 && response_codes !== 4'hf) ||
            updates < 1 || losses < 1 || held_rate < 8 || held_audit < 8 || held_reply < 2)
            $fatal(1,"incomplete coverage codes=%h modes=%h responses=%h",audit_codes,modes,response_codes);
        $display("PASS PPS discipline: %0d cycles, %0d samples, %0d updates, %0d audits, %0d lost, %0d/%0d/%0d rate/audit/reply stalls",
                 row,observed,updates,audits,losses,held_rate,held_audit,held_reply);
        $finish;
    end
endmodule

`default_nettype wire
