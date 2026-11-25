`timescale 1ns/1ps
`default_nettype none
`include "reconcile_defs.svh"

// Isolate incident recovery by adapter namespace. Each partition has its own
// table, epoch, retention and mailbox. Reports merge in round-robin order.
module drop_copy_partitions #(
    parameter integer NAMESPACES = 3,
    parameter integer SETS = 4,
    parameter integer WAIT_CYCLES = 24,
    parameter integer HISTORY_CYCLES = 40,
    parameter logic [31:0] RESET_TICK = 0,
    localparam integer NS_W = $clog2(NAMESPACES+1),
    localparam integer COUNT_W = $clog2(2*SETS+1)
) (
    input wire clk, rst,
    input wire s_primary_valid,
    output wire s_primary_ready,
    input wire [NS_W-1:0] s_primary_namespace,
    input wire [`DC_KEY_W-1:0] s_primary_key,
    input wire [`DC_PAYLOAD_W-1:0] s_primary_payload,

    input wire s_copy_valid,
    output wire s_copy_ready,
    input wire [NS_W-1:0] s_copy_namespace,
    input wire [`DC_KEY_W-1:0] s_copy_key,
    input wire [`DC_PAYLOAD_W-1:0] s_copy_payload,

    input wire s_epoch_valid,
    output wire s_epoch_ready,
    input wire [NS_W-1:0] s_epoch_namespace,
    input wire [15:0] s_new_epoch,

    output wire m_valid,
    input wire m_ready,
    output wire [NS_W-1:0] m_namespace,
    output wire [3:0] m_status,
    output wire [1:0] m_source,
    output wire [`DC_KEY_W-1:0] m_key,
    output wire [`DC_PAYLOAD_W-1:0] m_expected, m_observed,
    output wire [3:0] m_diff,
    output wire [31:0] m_age,
    output wire [COUNT_W-1:0] m_flushed,

    output wire [NAMESPACES*16-1:0] active_epochs,
    output wire [NAMESPACES*COUNT_W-1:0] pending_counts,
    output wire [NAMESPACES*320-1:0] namespace_metrics,
    output wire [NAMESPACES-1:0] namespace_incidents,
    output logic [31:0] bad_namespace_count
);
    if (NAMESPACES < 1) begin : g_invalid
        initial $fatal(1, "at least one namespace is required");
    end

    // The final arbiter endpoint carries invalid-address reports.
    localparam integer REPORT_W = NS_W + 4 + 2 + `DC_KEY_W + 2*`DC_PAYLOAD_W + 4 + 32 + COUNT_W;
    wire [REPORT_W-1:0] reports [0:NAMESPACES];
    wire [NAMESPACES:0] report_valid, report_ready;
    wire [NAMESPACES-1:0] primary_ready, copy_ready, epoch_ready;
    logic [NS_W-1:0] cursor, held_at, selected;
    logic held, have_report;
    logic grant_primary, grant_copy, grant_epoch;
    logic prefer_bad_copy;

    wire bad_primary = int'(s_primary_namespace) >= NAMESPACES;
    wire bad_copy = int'(s_copy_namespace) >= NAMESPACES;
    wire bad_epoch = int'(s_epoch_namespace) >= NAMESPACES;
    wire bad_primary_offer = s_primary_valid && bad_primary;
    wire bad_copy_offer = s_copy_valid && bad_copy;
    wire bad_epoch_offer = s_epoch_valid && bad_epoch;
    wire bad_room = !rst && (!report_valid[NAMESPACES] || report_ready[NAMESPACES]);

    logic bad_result_valid;
    logic [REPORT_W-1:0] bad_result;
    assign report_valid[NAMESPACES] = bad_result_valid;
    assign reports[NAMESPACES] = bad_result;

    assign s_primary_ready = !rst &&
        (bad_primary ? grant_primary : primary_ready[s_primary_namespace]);
    assign s_copy_ready = !rst &&
        (bad_copy ? grant_copy : copy_ready[s_copy_namespace]);
    assign s_epoch_ready = !rst &&
        (bad_epoch ? grant_epoch : epoch_ready[s_epoch_namespace]);

    always @* begin
        grant_epoch = bad_room && bad_epoch_offer;
        grant_copy = bad_room && !bad_epoch_offer && bad_copy_offer &&
                     (!bad_primary_offer || prefer_bad_copy);
        grant_primary = bad_room && !bad_epoch_offer && bad_primary_offer && !grant_copy;
    end

    wire [NS_W-1:0] bad_ns = grant_epoch ? s_epoch_namespace :
                              grant_copy ? s_copy_namespace : s_primary_namespace;
    wire [1:0] bad_source = grant_epoch ? 2'd3 : grant_copy ? 2'd1 : 2'd0;
    wire [`DC_KEY_W-1:0] bad_key = grant_epoch ? {s_new_epoch,58'd0} :
        grant_copy ? s_copy_key : s_primary_key;
    wire [`DC_PAYLOAD_W-1:0] bad_payload = grant_epoch ? '0 :
        grant_copy ? s_copy_payload : s_primary_payload;

    always_ff @(posedge clk) begin
        if (rst) begin
            bad_result_valid <= 1'b0;
            bad_namespace_count <= '0;
            prefer_bad_copy <= 1'b0;
        end else if (bad_room) begin
            bad_result_valid <= grant_epoch || grant_copy || grant_primary;
            if (grant_epoch || grant_copy || grant_primary) begin
                bad_result <= {bad_ns, `DC_BAD_NAMESPACE, bad_source, bad_key,
                               `DC_PAYLOAD_W'(0), bad_payload, 4'd0, 32'd0, COUNT_W'(0)};
                if (!(&bad_namespace_count))
                    bad_namespace_count <= bad_namespace_count + 1'b1;
                if (!grant_epoch)
                    prefer_bad_copy <= grant_primary;
            end
        end
    end

    for (genvar domain = 0; domain < NAMESPACES; domain++) begin : g_domain
        localparam logic [NS_W-1:0] NUMBER = NS_W'(domain);
        wire feed_primary = s_primary_valid && s_primary_namespace == NUMBER;
        wire feed_copy = s_copy_valid && s_copy_namespace == NUMBER;
        wire restart = s_epoch_valid && s_epoch_namespace == NUMBER;
        wire [15:0] active_epoch;
        wire [COUNT_W-1:0] pending;
        wire [3:0] status;
        wire [1:0] source;
        wire [`DC_KEY_W-1:0] key;
        wire [`DC_PAYLOAD_W-1:0] expected, observed;
        wire [3:0] diff;
        wire [31:0] age;
        wire [COUNT_W-1:0] flushed;
        wire [31:0] tick, observed_count, matched_count, conflict_count, duplicate_count,
                    missing_count, late_count, full_count, rejected_count, flushed_count;

        drop_copy_reconciliation #(
            .SETS(SETS), .WAIT_CYCLES(WAIT_CYCLES), .HISTORY_CYCLES(HISTORY_CYCLES),
            .RESET_TICK(RESET_TICK)
        ) ledger (
            .clk, .rst, .s_primary_valid(feed_primary), .s_primary_ready(primary_ready[domain]),
            .s_primary_key, .s_primary_payload, .s_copy_valid(feed_copy),
            .s_copy_ready(copy_ready[domain]), .s_copy_key, .s_copy_payload,
            .s_epoch_valid(restart), .s_epoch_ready(epoch_ready[domain]), .s_new_epoch,
            .m_valid(report_valid[domain]), .m_ready(report_ready[domain]), .m_status(status),
            .m_source(source), .m_key(key), .m_expected(expected), .m_observed(observed),
            .m_diff(diff), .m_age(age), .m_flushed(flushed), .active_epoch,
            .pending_count(pending), .tick, .observed_count, .matched_count, .conflict_count,
            .duplicate_count, .missing_count, .late_count, .full_count, .rejected_count,
            .flushed_count
        );

        assign reports[domain] = {NUMBER, status, source, key, expected, observed, diff, age, flushed};
        assign active_epochs[domain*16 +: 16] = active_epoch;
        assign pending_counts[domain*COUNT_W +: COUNT_W] = pending;
        assign namespace_metrics[domain*320 +: 320] = {
            tick, flushed_count, rejected_count, full_count, late_count, missing_count,
            duplicate_count, conflict_count, matched_count, observed_count
        };

        // The incident is visible as soon as a child's diagnostic exists,
        // independently of audit arbitration. Retain it until a successful
        // restart for this domain. Continue collecting diagnostic evidence.
        logic incident;
        wire critical_report = report_valid[domain] &&
            (status == `DC_PAIR_CONFLICT || status == `DC_DUP_CONFLICT ||
             status == `DC_MISSING_COPY || status == `DC_MISSING_PRIMARY || status == `DC_FULL);
        wire restarted = restart && epoch_ready[domain] && s_new_epoch > active_epoch;
        assign namespace_incidents[domain] = !rst && (incident || critical_report);

        always_ff @(posedge clk) begin
            if (rst || restarted)
                incident <= 1'b0;
            else if (critical_report)
                incident <= 1'b1;
        end
    end

    always @* begin : choose_report
        integer scan;
        selected = held ? held_at : cursor;
        have_report = held && report_valid[held_at];
        scan = 0;
        if (!held) begin
            for (integer offset = 0; offset <= NAMESPACES; offset++) begin
                scan = int'(cursor) + offset;
                if (scan > NAMESPACES)
                    scan = scan - NAMESPACES - 1;
                if (!have_report && report_valid[scan]) begin
                    selected = NS_W'(scan);
                    have_report = 1'b1;
                end
            end
        end
    end

    assign m_valid = !rst && have_report;
    assign {m_namespace,m_status,m_source,m_key,m_expected,m_observed,m_diff,m_age,m_flushed}
        = m_valid ? reports[selected] : '0;

    for (genvar endpoint = 0; endpoint <= NAMESPACES; endpoint++) begin : g_ready
        assign report_ready[endpoint] = m_valid && m_ready && selected == NS_W'(endpoint);
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            cursor <= '0;
            held <= 1'b0;
            held_at <= '0;
        end else if (m_valid) begin
            held <= !m_ready;
            if (m_ready)
                cursor <= selected == NS_W'(NAMESPACES) ? '0 : selected + 1'b1;
            else
                held_at <= selected;
        end
    end
endmodule

`default_nettype wire
