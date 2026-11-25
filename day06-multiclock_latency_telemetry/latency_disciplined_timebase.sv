`timescale 1ns/1ps
`default_nettype none

// Optional integration for deployments that source time from the PPS servo.
// A legacy timebase can still receive host-written periods without this top.
module latency_disciplined_timebase #(
    parameter integer REV_W = 16,
    parameter integer AUDIT_DEPTH = 8,
    parameter logic [63:0] INITIAL_PERIOD = 64'h0000000800000000,
    localparam integer OCC_W = $clog2(AUDIT_DEPTH+1)
) (
    input wire clk, rst, pps_async,
    output wire [63:0] now_tick, gray_ticks, nanoseconds,
    output wire [31:0] subnanoseconds,
    output wire saturated, invalid_rate,
    output wire pps_observed,
    output wire [63:0] pps_ticks, pps_nanoseconds,
    output wire [31:0] pps_missed,

    input wire cfg_valid,
    output wire cfg_ready,
    input wire [1:0] cfg_op,
    input wire [REV_W-1:0] cfg_expected_rev,
    input wire cfg_enable,
    input wire [63:0] cfg_reference_ns, cfg_min_ticks, cfg_max_ticks, cfg_timeout,
    input wire [63:0] cfg_max_age, cfg_jitter, cfg_min_period, cfg_max_period, cfg_max_step,
    input wire [7:0] cfg_lock_windows,
    output wire rsp_valid,
    input wire rsp_ready,
    output wire [1:0] rsp_op,
    output wire [2:0] rsp_status,
    output wire [REV_W-1:0] rsp_rev,
    output wire [63:0] rsp_period,
    output wire [1:0] rsp_mode,

    output wire audit_valid,
    input wire audit_ready,
    output wire [REV_W-1:0] audit_rev,
    output wire [63:0] audit_sequence,
    output wire [325:0] audit_data,
    output wire [OCC_W-1:0] audit_occupancy,
    output wire [63:0] audit_cursor,
    output wire [1:0] mode,
    output wire anchor_valid,
    output wire [63:0] anchor_tick, active_period,
    output wire [1:0] interval_count,
    output wire [7:0] stable_windows,
    output wire [31:0] sampled, invalid_samples, usable_intervals, applied_rates,
    output wire [31:0] rejected_periods, holdovers, lost_audits
);
    wire pps_valid, pps_ready, rate_valid, rate_ready;
    wire [63:0] rate_period;
    assign pps_observed = pps_valid && pps_ready;

    latency_timebase #(
        .INITIAL_NS(INITIAL_PERIOD[63:32]), .INITIAL_FRACTION(INITIAL_PERIOD[31:0])
    ) timebase (
        .clk, .reset(rst), .pps_async, .rate_valid, .rate_ready,
        .rate_ns(rate_period[63:32]), .rate_fraction(rate_period[31:0]),
        .ticks(now_tick), .gray_ticks, .nanoseconds, .subnanoseconds,
        .saturated, .invalid_rate, .pps_valid, .pps_ready,
        .pps_ticks, .pps_nanoseconds, .pps_missed
    );

    latency_pps_discipline #(
        .REV_W(REV_W), .AUDIT_DEPTH(AUDIT_DEPTH), .INITIAL_PERIOD(INITIAL_PERIOD)
    ) discipline (
        .clk, .rst, .now_tick,
        .cfg_valid, .cfg_ready, .cfg_op, .cfg_expected_rev, .cfg_enable,
        .cfg_reference_ns, .cfg_min_ticks, .cfg_max_ticks, .cfg_timeout, .cfg_max_age,
        .cfg_jitter, .cfg_min_period, .cfg_max_period, .cfg_max_step, .cfg_lock_windows,
        .rsp_valid, .rsp_ready, .rsp_op, .rsp_status, .rsp_rev, .rsp_period, .rsp_mode,
        .sample_valid(pps_valid), .sample_ready(pps_ready), .sample_tick(pps_ticks),
        .rate_valid, .rate_ready, .rate_period,
        .audit_valid, .audit_ready, .audit_rev, .audit_sequence, .audit_data, .audit_occupancy, .audit_cursor,
        .mode, .anchor_valid, .anchor_tick, .active_period, .interval_count, .stable_windows,
        .sampled, .invalid_samples, .usable_intervals, .applied_rates, .rejected_periods,
        .holdovers, .lost_audits
    );
endmodule

`default_nettype wire
