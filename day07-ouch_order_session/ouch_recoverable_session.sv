`timescale 1ns/1ps
`default_nettype none

module ouch_recoverable_session #(
    parameter integer ORDER_SLOTS = 8,
    parameter integer TX_HEARTBEAT_TICKS = 1_000,
    parameter integer RX_TIMEOUT_TICKS = 5_000,
    parameter integer REPLAY_HISTORY_DEPTH = 8
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
    output wire [7:0] pending_count,
    input wire cp_cmd_valid,
    output wire cp_cmd_ready,
    input wire [1:0] cp_cmd_op,
    input wire [63:0] cp_image_id,
    input wire cp_abort_import,
    input wire cp_in_valid,
    output wire cp_in_ready,
    input wire [31:0] cp_in_data,
    input wire cp_in_last,
    output wire cp_out_valid,
    input wire cp_out_ready,
    output wire [31:0] cp_out_data,
    output wire cp_out_last,
    output wire cp_result_valid,
    input wire cp_result_ready,
    output wire [3:0] cp_result_status,
    output wire [1:0] cp_result_op,
    output wire [63:0] cp_result_id,
    output wire [7:0] cp_bad_slot,
    output wire checkpoint_busy,
    output wire boot_open
);
    import ouch_wire_pkg::*;

    if (ORDER_SLOTS < 1 || ORDER_SLOTS > 255 || REPLAY_HISTORY_DEPTH < 1) begin : invalid_geometry
        initial $fatal(1,"recoverable sessions require 1..255 order slots and exact replay");
    end

    wire block_session, archive_can_export, archive_can_import;
    wire restore_slot_valid, restore_meta_valid;
    wire [7:0] inspect_slot, restore_slot;
    order_state_t inspected_order, restore_order;
    checkpoint_meta_t current_meta, restore_meta;
    slot_view_t native_view;
    wire [31:0] native_highest;
    wire [7:0] native_live, native_pending;
    wire [79:0] native_session;
    wire [63:0] native_next;
    wire native_market;
    logic socket_rearmed;
    wire quiescent = !transport_up;
    wire effective_link = transport_up && !block_session && socket_rearmed;
    wire maintenance = block_session || (cp_cmd_valid && cp_cmd_ready && quiescent);

    // A socket raised during maintenance cannot become the post-recovery
    // connection. Require a sampled down boundary after the maintenance gate.
    always_ff @(posedge clk) begin
        if (rst || block_session)
            socket_rearmed <= 1'b0;
        else if (!transport_up)
            socket_rearmed <= 1'b1;
    end

    assign session_id = boot_open ? native_session : BLANK_SESSION;
    assign next_sequence = boot_open ? native_next : 64'd1;
    assign market_open = boot_open && native_market;
    assign query_view = boot_open ? native_view : '0;
    assign highest_order_ref = boot_open ? native_highest : '0;
    assign live_count = boot_open ? native_live : '0;
    assign pending_count = boot_open ? native_pending : '0;

    ouch_checkpoint_stream #(.ORDER_SLOTS(ORDER_SLOTS)) archive (
        .allow_import(archive_can_import), .allow_export(archive_can_export), .*
    );

    ouch_session_engine #(
        .ORDER_SLOTS(ORDER_SLOTS), .TX_HEARTBEAT_TICKS(TX_HEARTBEAT_TICKS),
        .RX_TIMEOUT_TICKS(RX_TIMEOUT_TICKS), .REPLAY_HISTORY_DEPTH(REPLAY_HISTORY_DEPTH)
    ) engine (
        .transport_up(effective_link), .session_id(native_session), .next_sequence(native_next),
        .market_open(native_market), .query_view(native_view), .highest_order_ref(native_highest),
        .live_count(native_live), .pending_count(native_pending),
        .archive_meta(current_meta), .archive_inspect_slot(inspect_slot), .archive_inspected(inspected_order),
        .archive_restore_slot_valid(restore_slot_valid), .archive_restore_slot(restore_slot),
        .archive_restore_state(restore_order), .archive_restore_meta_valid(restore_meta_valid),
        .archive_restore_meta(restore_meta), .*
    );
endmodule

`default_nettype wire
