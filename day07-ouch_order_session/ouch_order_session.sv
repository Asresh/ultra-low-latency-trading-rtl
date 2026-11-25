`timescale 1ns/1ps
`default_nettype none

module ouch_order_session #(
    parameter integer ORDER_SLOTS = 8,
    parameter integer TX_HEARTBEAT_TICKS = 1_000,
    parameter integer RX_TIMEOUT_TICKS = 5_000,
    parameter integer REPLAY_HISTORY_DEPTH = 0
) (
    input wire clk,
    input wire rst,
    input wire transport_up,
    input wire time_tick,
    input wire kill_req,
    output wire kill_latched,
    output wire close_transport,
    input wire s_login_valid,
    output logic s_login_ready,
    input ouch_wire_pkg::login_t s_login,
    input wire s_logout_valid,
    output logic s_logout_ready,
    input wire s_cmd_valid,
    output logic s_cmd_ready,
    input ouch_wire_pkg::command_t s_cmd,
    input wire s_rx_valid,
    output wire s_rx_ready,
    input wire [63:0] s_rx_data,
    input wire [7:0] s_rx_keep,
    output wire m_tx_valid,
    input wire m_tx_ready,
    output wire [63:0] m_tx_data,
    output wire [7:0] m_tx_keep,
    output wire m_tx_last,
    output wire m_notice_valid,
    input wire m_notice_ready,
    output ouch_wire_pkg::notice_t m_notice,
    input wire [7:0] query_slot,
    output ouch_wire_pkg::slot_view_t query_view,
    output ouch_wire_pkg::metrics_t metrics,
    output ouch_wire_pkg::session_state_t session_state,
    output ouch_wire_pkg::status_t last_fault,
    output wire [79:0] session_id,
    output wire [63:0] next_sequence,
    output wire replaying,
    output wire market_open,
    output wire [31:0] highest_order_ref,
    output wire [7:0] live_count,
    output wire [7:0] pending_count
);
    /* verilator lint_off UNUSEDSIGNAL */
    wire unused_can_export, unused_can_import;
    ouch_wire_pkg::checkpoint_meta_t unused_meta;
    ouch_wire_pkg::order_state_t unused_inspection;
    /* verilator lint_on UNUSEDSIGNAL */

    ouch_session_engine #(
        .ORDER_SLOTS(ORDER_SLOTS), .TX_HEARTBEAT_TICKS(TX_HEARTBEAT_TICKS),
        .RX_TIMEOUT_TICKS(RX_TIMEOUT_TICKS), .REPLAY_HISTORY_DEPTH(REPLAY_HISTORY_DEPTH)
    ) engine (
        .maintenance(1'b0), .archive_can_export(unused_can_export), .archive_can_import(unused_can_import),
        .archive_meta(unused_meta), .archive_inspect_slot(8'd0), .archive_inspected(unused_inspection),
        .archive_restore_slot_valid(1'b0), .archive_restore_slot(8'd0), .archive_restore_state('0),
        .archive_restore_meta_valid(1'b0), .archive_restore_meta('0), .*
    );
endmodule

`default_nettype wire
