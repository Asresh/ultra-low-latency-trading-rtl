`timescale 1ns/1ps
`default_nettype none

module ouch_order_table #(
    parameter integer ORDER_SLOTS = 8
) (
    input wire clk,
    input wire rst,
    input ouch_wire_pkg::command_t command,
    input ouch_wire_pkg::status_t cmd_session_status,
    input ouch_wire_pkg::status_t admission_status,
    input wire cmd_commit,
    output ouch_wire_pkg::notice_t cmd_notice,
    output ouch_wire_pkg::packet_t cmd_packet,
    output wire cmd_sends,
    input ouch_wire_pkg::inbound_t inbound,
    input wire [63:0] sequence_id,
    input wire rx_commit,
    output ouch_wire_pkg::notice_t rx_notice,
    input wire [7:0] query_slot,
    output ouch_wire_pkg::slot_view_t query_view,
    output wire [31:0] highest_order_ref,
    output wire [7:0] live_count,
    output wire [7:0] pending_count
);
    /* verilator lint_off UNUSEDSIGNAL */
    ouch_wire_pkg::order_state_t unused_inspection;
    /* verilator lint_on UNUSEDSIGNAL */

    ouch_order_table_engine #(.ORDER_SLOTS(ORDER_SLOTS)) engine (
        .inspect_slot(8'd0), .inspected(unused_inspection),
        .restore_slot_valid(1'b0), .restore_slot(8'd0), .restore_state('0),
        .restore_ref_valid(1'b0), .restore_high_water(32'd0), .*
    );
endmodule

`default_nettype wire
