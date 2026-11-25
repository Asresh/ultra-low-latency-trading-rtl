`timescale 1ns/1ps
`default_nettype none
`include "reconcile_defs.svh"

// Lossless journal for accepted operations and diagnostic publication. The
// audit mailbox gates external handshakes, but it never stops reconciliation
// time. A separate public mailbox retains diagnostics through sink stalls.
module drop_copy_investigator #(
    parameter integer NAMESPACES = 3,
    parameter integer SETS = 4,
    parameter integer WAIT_CYCLES = 24,
    parameter integer HISTORY_CYCLES = 40,
    parameter logic [31:0] RESET_TICK = 0,
    localparam integer NS_W = $clog2(NAMESPACES+1),
    localparam integer COUNT_W = $clog2(2*SETS+1),
    localparam integer REPORT_W = NS_W + 4 + 2 + `DC_KEY_W + 2*`DC_PAYLOAD_W + 4 + 32 + COUNT_W,
    localparam integer EVENT_W = NS_W + `DC_KEY_W + `DC_PAYLOAD_W,
    localparam integer EPOCH_REQ_W = NS_W + 16,
    localparam integer MARK_W = 1 + 32 + 32 + NAMESPACES*(16+COUNT_W+1) + 32,
    localparam integer RECORD_W = 3*64 + 5 + 2*EVENT_W + EPOCH_REQ_W + REPORT_W + MARK_W
) (
    input wire clk, rst,
    input wire [63:0] run_identity,

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

    input wire mark_valid,
    output wire mark_ready,
    input wire [31:0] mark_tag,
    input wire mark_terminal,

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
    output logic [63:0] m_record_index,

    output wire audit_valid,
    input wire audit_ready,
    output logic [RECORD_W-1:0] audit_record,
    output logic [63:0] audit_index,
    output logic sealed,

    output wire [NAMESPACES*16-1:0] active_epochs,
    output wire [NAMESPACES*COUNT_W-1:0] pending_counts,
    output wire [NAMESPACES*320-1:0] namespace_metrics,
    output wire [NAMESPACES-1:0] namespace_incidents,
    output wire [31:0] bad_namespace_count
);
    logic [63:0] identity, cycle_id;
    logic audit_pending, diagnostic_pending;
    logic [REPORT_W-1:0] diagnostic;
    wire core_primary_ready, core_copy_ready, core_epoch_ready, core_report_valid;
    wire [NS_W-1:0] core_namespace;
    wire [3:0] core_status, core_diff;
    wire [1:0] core_source;
    wire [`DC_KEY_W-1:0] core_key;
    wire [`DC_PAYLOAD_W-1:0] core_expected, core_observed;
    wire [31:0] core_age;
    wire [COUNT_W-1:0] core_flushed;

    wire audit_room = !audit_pending || audit_ready;
    wire report_room = !diagnostic_pending || m_ready;
    wire clock_open = !rst && !sealed && identity != 0 && !(&cycle_id) && !(&audit_index);
    wire event_room = clock_open && audit_room;
    assign mark_ready = event_room;
    wire capture_mark = mark_valid && mark_ready;
    wire terminal_edge = capture_mark && mark_terminal;
    wire accept_core = event_room && !terminal_edge;
    wire core_report_ready = accept_core && report_room;
    assign s_primary_ready = accept_core && core_primary_ready;
    assign s_copy_ready = accept_core && core_copy_ready;
    assign s_epoch_ready = accept_core && core_epoch_ready;
    assign audit_valid = !rst && audit_pending;
    assign m_valid = !rst && diagnostic_pending;

    wire capture_primary = s_primary_valid && s_primary_ready;
    wire capture_copy = s_copy_valid && s_copy_ready;
    wire capture_epoch = s_epoch_valid && s_epoch_ready;
    wire capture_report = core_report_valid && core_report_ready;
    wire [4:0] capture_mask = {capture_mark,capture_report,capture_epoch,capture_copy,capture_primary};
    wire any_capture = |capture_mask;
    wire [63:0] next_audit = audit_index + 1'b1;
    wire [EVENT_W-1:0] primary_image = capture_primary ?
        {s_primary_namespace,s_primary_key,s_primary_payload} : '0;
    wire [EVENT_W-1:0] copy_image = capture_copy ?
        {s_copy_namespace,s_copy_key,s_copy_payload} : '0;
    wire [EPOCH_REQ_W-1:0] epoch_image = capture_epoch ?
        {s_epoch_namespace,s_new_epoch} : '0;
    wire [REPORT_W-1:0] report_image = {core_namespace,core_status,core_source,core_key,
        core_expected,core_observed,core_diff,core_age,core_flushed};
    wire [MARK_W-1:0] mark_image = capture_mark ?
        {mark_terminal,mark_tag,namespace_metrics[319:288],active_epochs,pending_counts,
         namespace_incidents,bad_namespace_count} : '0;

    assign {m_namespace,m_status,m_source,m_key,m_expected,m_observed,m_diff,m_age,m_flushed} = diagnostic;

    drop_copy_partitions #(
        .NAMESPACES(NAMESPACES), .SETS(SETS), .WAIT_CYCLES(WAIT_CYCLES), .HISTORY_CYCLES(HISTORY_CYCLES),
        .RESET_TICK(RESET_TICK)
    ) ledger (
        .clk, .rst, .s_primary_valid(accept_core && s_primary_valid), .s_primary_ready(core_primary_ready),
        .s_primary_namespace, .s_primary_key, .s_primary_payload,
        .s_copy_valid(accept_core && s_copy_valid), .s_copy_ready(core_copy_ready),
        .s_copy_namespace, .s_copy_key, .s_copy_payload,
        .s_epoch_valid(accept_core && s_epoch_valid), .s_epoch_ready(core_epoch_ready),
        .s_epoch_namespace, .s_new_epoch,
        .m_valid(core_report_valid), .m_ready(core_report_ready), .m_namespace(core_namespace),
        .m_status(core_status), .m_source(core_source), .m_key(core_key),
        .m_expected(core_expected), .m_observed(core_observed), .m_diff(core_diff),
        .m_age(core_age), .m_flushed(core_flushed),
        .active_epochs, .pending_counts, .namespace_metrics, .namespace_incidents, .bad_namespace_count
    );

    always_ff @(posedge clk) begin
        if (rst) begin
            identity <= run_identity;
            cycle_id <= '0;
            audit_index <= '0;
            audit_pending <= 1'b0;
            diagnostic_pending <= 1'b0;
            audit_record <= '0;
            diagnostic <= '0;
            m_record_index <= '0;
            sealed <= 1'b0;
        end else begin
            if (!(&cycle_id))
                cycle_id <= cycle_id + 1'b1;
            if (audit_ready)
                audit_pending <= 1'b0;
            if (m_ready)
                diagnostic_pending <= 1'b0;
            if (any_capture) begin
                audit_pending <= 1'b1;
                audit_index <= next_audit;
                audit_record <= {identity,next_audit,cycle_id,capture_mask,primary_image,copy_image,
                    epoch_image,capture_report ? report_image : REPORT_W'(0),mark_image};
            end
            if (capture_report) begin
                diagnostic_pending <= 1'b1;
                diagnostic <= report_image;
                m_record_index <= next_audit;
            end
            if (terminal_edge)
                sealed <= 1'b1;
        end
    end

`ifdef FORMAL
    logic initialized = 1'b0;
    always @(posedge clk) begin
        initialized <= 1'b1;
        if (initialized && !rst && !$past(rst)) begin
            if ($past(audit_valid && !audit_ready))
                assert(audit_valid && $stable(audit_record));
            if ($past(m_valid && !m_ready))
                assert(m_valid && $stable({diagnostic,m_record_index}));
            assert(audit_index == $past(audit_index) + 64'($past(any_capture)));
            if (sealed)
                assert(!s_primary_ready && !s_copy_ready && !s_epoch_ready && !mark_ready);
            if (terminal_edge)
                assert(capture_mask == 5'b10000);
        end
    end
`endif
endmodule

`default_nettype wire
