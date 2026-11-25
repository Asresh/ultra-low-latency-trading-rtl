`timescale 1ns/1ps
`default_nettype none

module tb_latency_disciplined_timebase;
    localparam integer REV_W = 4, AUDIT_DEPTH = 8, OCC_W = 4;
    localparam logic [63:0] INITIAL = 64'h0000000800000000;
    localparam logic [63:0] STEP = 64'h0000000010000000;
    logic clk = 0, rst = 1, pps_async = 0;
    logic cfg_valid = 0, cfg_enable = 1, rsp_ready = 1, audit_ready = 1;
    logic [1:0] cfg_op = 0;
    logic [REV_W-1:0] cfg_expected_rev = 0;
    logic [63:0] cfg_reference_ns = 1000, cfg_min_ticks = 95, cfg_max_ticks = 160;
    logic [63:0] cfg_timeout = 480, cfg_max_age = 35, cfg_jitter = 3;
    logic [63:0] cfg_min_period = 64'h0000000500000000, cfg_max_period = 64'h0000000c00000000;
    logic [63:0] cfg_max_step = STEP;
    logic [7:0] cfg_lock_windows = 1;
    wire [63:0] now_tick, gray_ticks, nanoseconds, pps_ticks, pps_nanoseconds;
    wire [31:0] subnanoseconds, pps_missed;
    wire saturated, invalid_rate, pps_observed, cfg_ready, rsp_valid;
    wire [1:0] rsp_op, rsp_mode, mode, interval_count;
    wire [2:0] rsp_status;
    wire [REV_W-1:0] rsp_rev, audit_rev;
    wire [63:0] rsp_period, anchor_tick, active_period, audit_sequence, audit_cursor;
    wire anchor_valid, audit_valid;
    wire [325:0] audit_data;
    wire [3:0] audit_code = audit_data[325:322];
    wire [63:0] audit_effective_tick = audit_data[319:256];
    wire [OCC_W-1:0] audit_occupancy;
    wire [7:0] stable_windows;
    wire [31:0] sampled, invalid_samples, usable_intervals, applied_rates;
    wire [31:0] rejected_periods, holdovers, lost_audits;

    always #5 clk = !clk;
    latency_disciplined_timebase #(.REV_W(REV_W), .AUDIT_DEPTH(AUDIT_DEPTH), .INITIAL_PERIOD(INITIAL)) dut (.*);

    integer audits = 0, time_trace = 0, metadata = 0;
    integer count_up = 0, count_down = 0, count_samples = 0, count_audits = 0;
    logic [63:0] prior_period = INITIAL;
    logic [95:0] reference_q32 = 0;
    logic [63:0] held_gray = 0;
    logic [63:0] expected_period;

    always @(negedge clk) begin
        if (!rst)
            audit_ready = now_tick[3:0] != 4'd4 && now_tick[3:0] != 4'd5;
    end

    always @(posedge clk) begin
        if (rst) begin
            reference_q32 = 0;
            prior_period = INITIAL;
            held_gray = 0;
        end else begin
            reference_q32 += 96'(prior_period);
            expected_period = prior_period;
            if (dut.rate_valid && dut.rate_ready) begin
                expected_period = dut.rate_period;
                if (expected_period > prior_period) begin
                    if (expected_period - prior_period > STEP)
                        $fatal(1,"positive period correction exceeded slew");
                    count_up++;
                end else if (prior_period > expected_period) begin
                    if (prior_period - expected_period > STEP)
                        $fatal(1,"negative period correction exceeded slew");
                    count_down++;
                end else begin
                    $fatal(1,"identical period is not a correction");
                end
            end
            count_samples += pps_observed;
            if (audit_valid && audit_ready) begin
                if (audit_rev != 1)
                    $fatal(1,"live audit did not carry the programmed policy version");
                $fdisplay(audits,"%0d %0d %082h",audit_sequence,audit_rev,audit_data);
                count_audits++;
            end
            #1;
            if ({nanoseconds,subnanoseconds} !== reference_q32 || active_period !== expected_period)
                $fatal(1,"frequency discipline modified existing elapsed accumulations");
            if ($countones(gray_ticks ^ held_gray) != 1)
                $fatal(1,"PPS discipline compromised raw Gray tick continuity");
            held_gray = gray_ticks;
            prior_period = expected_period;
            $fdisplay(time_trace,"%0d %024h",now_tick,{nanoseconds,subnanoseconds});
        end
    end

    task automatic pulse_at(input logic [63:0] tick);
        while (now_tick < tick)
            @(negedge clk);
        #2 pps_async = 1;
        repeat (3) @(negedge clk);
        #1 pps_async = 0;
    endtask

    initial begin
        string out_dir, waves;
        logic [63:0] base, last_period;
        if (!$value$plusargs("out=%s",out_dir))
            out_dir = "build";
        audits = $fopen({out_dir,"/pps-discipline-audits.txt"},"w");
        time_trace = $fopen({out_dir,"/pps-discipline-clock.txt"},"w");
        metadata = $fopen({out_dir,"/pps-discipline-audit-meta.txt"},"w");
        if (!audits || !time_trace || !metadata)
            $fatal(1,"cannot create checked PPS capture");
        if ($value$plusargs("waves=%s",waves)) begin
            $dumpfile(waves);
            $dumpvars(1,tb_latency_disciplined_timebase);
            $dumpvars(1,tb_latency_disciplined_timebase.dut);
        end
        repeat (4) @(negedge clk);
        rst = 0;
        cfg_valid = 1;
        do @(posedge clk); while (!cfg_ready);
        @(negedge clk);
        cfg_valid = 0;
        if (!rsp_valid || rsp_status != 0 || rsp_rev != 1 || rsp_mode != 1)
            $fatal(1,"cannot install live PPS policy");

        base = now_tick + 12;
        pulse_at(base);
        pulse_at(base+120);
        pulse_at(base+145);                                      // Early glitch outside a calculation.
        pulse_at(base+240);
        pulse_at(base+360);
        pulse_at(base+374);                                      // Queue while the divider is busy.
        pulse_at(base+388);                                      // Native PPS overrun while that slot holds.
        pulse_at(base+480);
        pulse_at(base+600);
        pulse_at(base+730);
        pulse_at(base+860);
        pulse_at(base+990);
        pulse_at(base+1120);
        while (now_tick < base+1120+180)
            @(negedge clk);
        if (mode != 2 || count_up < 2 || count_down < 1)
            $fatal(1,"did not lock with both positive and negative slew");
        last_period = active_period;
        while (now_tick < base+1120+500)
            @(negedge clk);
        if (mode != 3 || !holdovers || active_period !== last_period)
            $fatal(1,"raw PPS gap did not preserve the held live period");

        base = now_tick + 8;
        pulse_at(base);
        pulse_at(base+130);
        pulse_at(base+260);
        pulse_at(base+390);
        while (now_tick < base+390+155)
            @(negedge clk);
        if (mode != 2 || pps_missed < 1 || holdovers != 1 || invalid_samples < 2 ||
            lost_audits != 0 || audit_occupancy != 0 || rejected_periods != 0 ||
            applied_rates != count_up+count_down || count_audits < count_samples ||
            saturated || invalid_rate)
            $fatal(1,"closed-loop calibration counters disagree");
        $fdisplay(metadata,"%0d %0d",lost_audits,audit_cursor);
        $fclose(metadata);
        $fclose(audits);
        $fclose(time_trace);
        $display("PASS live PPS discipline: %0d raw samples, %0d audits, %0d up/%0d down adjustments, %0d pps overruns, %0d holdover",
                 count_samples,count_audits,count_up,count_down,pps_missed,holdovers);
        $finish;
    end

    initial begin
        #50000;
        $fatal(1,"live PPS simulation timed out");
    end
endmodule

`default_nettype wire
