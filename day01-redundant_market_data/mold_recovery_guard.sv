`timescale 1ns/1ps
`default_nettype none

// A transport adapter arms a lease before transmitting the receiver's
// request. Its return path supplies the same local slot, epoch and token.
module mold_recovery_guard #(
    parameter integer SLOTS = 3,
    parameter integer PACKET_BYTES = 256,
    parameter integer MESSAGE_BYTES = 64,
    parameter integer BLOCKS = 16,
    parameter integer WINDOW = 16,
    parameter integer EPOCH_W = 16,
    parameter integer TOKEN_W = 16,
    localparam integer KEY_W = $clog2(SLOTS+1)
) (
    input wire clk, rst,

    input wire cmd_valid,
    output wire cmd_ready,
    input wire [1:0] cmd_op,
    input wire [KEY_W-1:0] cmd_slot,
    input wire [EPOCH_W-1:0] cmd_epoch,
    input wire cmd_enable,
    input wire [TOKEN_W-1:0] cmd_token,
    input wire [159:0] cmd_request,
    input wire [31:0] cmd_ttl,
    output wire rsp_valid,
    input wire rsp_ready,
    output wire [1:0] rsp_op,
    output wire [KEY_W-1:0] rsp_slot,
    output wire [3:0] rsp_status,
    output wire [EPOCH_W-1:0] rsp_epoch,
    output wire rsp_enabled,
    output wire [TOKEN_W-1:0] rsp_token,
    output wire [2:0] rsp_state,
    output wire [159:0] rsp_request,
    output wire [WINDOW-1:0] rsp_coverage,
    output wire [31:0] rsp_ttl,

    input wire rx_valid,
    output wire rx_ready,
    input wire [63:0] rx_data,
    input wire [7:0] rx_keep,
    input wire rx_first, rx_last,
    input wire [KEY_W-1:0] rx_slot,
    input wire [EPOCH_W-1:0] rx_epoch,
    input wire [TOKEN_W-1:0] rx_token,

    output wire replay_valid,
    input wire replay_ready,
    output wire [63:0] replay_data,
    output wire [7:0] replay_keep,
    output wire replay_first, replay_last,
    output wire [KEY_W-1:0] replay_slot,
    output wire [EPOCH_W-1:0] replay_epoch,
    output wire [TOKEN_W-1:0] replay_token,

    output wire audit_valid,
    input wire audit_ready,
    output wire [KEY_W-1:0] audit_slot,
    output wire [EPOCH_W-1:0] audit_epoch,
    output wire [TOKEN_W-1:0] audit_token,
    output wire [3:0] audit_status,
    output wire [63:0] audit_sequence,
    output wire [15:0] audit_count, audit_bytes,
    output wire [WINDOW-1:0] audit_coverage,
    output wire audit_completed,

    input wire [KEY_W-1:0] stat_slot,
    input wire [4:0] stat_index,
    output wire stat_valid,
    output wire [31:0] stat_value,
    output wire [31:0] total_frames, rejected_frames, unknown_slots
);
    wire descriptor_valid, descriptor_ready, descriptor_pass;
    wire [KEY_W-1:0] packet_slot;
    wire [EPOCH_W-1:0] packet_epoch;
    wire [TOKEN_W-1:0] packet_token;
    wire [79:0] packet_session;
    wire [63:0] packet_sequence;
    wire [15:0] packet_count, packet_bytes;
    wire [3:0] frame_status;

    assign replay_slot = replay_valid ? packet_slot : '0;
    assign replay_epoch = replay_valid ? packet_epoch : '0;
    assign replay_token = replay_valid ? packet_token : '0;

    mold_recovery_frame #(
        .PACKET_BYTES(PACKET_BYTES), .MESSAGE_BYTES(MESSAGE_BYTES), .BLOCKS(BLOCKS),
        .KEY_W(KEY_W), .EPOCH_W(EPOCH_W), .TOKEN_W(TOKEN_W)
    ) frame (
        .clk, .rst, .rx_valid, .rx_ready, .rx_data, .rx_keep, .rx_first, .rx_last,
        .rx_slot, .rx_epoch, .rx_token, .descriptor_valid, .descriptor_ready, .descriptor_pass,
        .packet_slot, .packet_epoch, .packet_token, .packet_session,
        .packet_sequence, .packet_count, .packet_bytes, .frame_status,
        .replay_valid, .replay_ready, .replay_data, .replay_keep, .replay_first, .replay_last
    );

    mold_recovery_leases #(.SLOTS(SLOTS), .WINDOW(WINDOW), .EPOCH_W(EPOCH_W), .TOKEN_W(TOKEN_W)) leases (
        .clk, .rst, .cmd_valid, .cmd_ready, .cmd_op, .cmd_slot, .cmd_epoch, .cmd_enable,
        .cmd_token, .cmd_request, .cmd_ttl, .rsp_valid, .rsp_ready, .rsp_op, .rsp_slot,
        .rsp_status, .rsp_epoch, .rsp_enabled, .rsp_token, .rsp_state, .rsp_request,
        .rsp_coverage, .rsp_ttl,
        .descriptor_valid, .descriptor_ready, .descriptor_pass, .packet_slot, .packet_epoch,
        .packet_token, .packet_session, .packet_sequence, .packet_count, .packet_bytes,
        .frame_status, .replay_active(replay_valid),
        .audit_valid, .audit_ready, .audit_slot, .audit_epoch, .audit_token, .audit_status,
        .audit_sequence, .audit_count, .audit_bytes, .audit_coverage, .audit_completed,
        .stat_slot, .stat_index, .stat_valid, .stat_value, .total_frames, .rejected_frames, .unknown_slots
    );
endmodule

`default_nettype wire
