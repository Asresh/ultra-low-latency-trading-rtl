`timescale 1ns/1ps
`default_nettype none

module quote_inventory_manager #(
    parameter integer INSTRUMENTS=3,
    parameter integer LEVELS=128,
    parameter integer POS_W=24,
    parameter integer QTY_W=16,
    parameter integer RESERVE_W=24,
    parameter integer EPOCH_W=8,
    parameter integer SEQ_W=32,
    parameter integer REV_W=16,
    parameter integer TAG_W=16,
    localparam integer PRICE_W=$clog2(LEVELS+1),
    localparam integer INST_W=$clog2(INSTRUMENTS+1),
    localparam integer POLICY_W=4*POS_W+2*QTY_W+PRICE_W
) (
    input wire clk,
    input wire rst,
    input wire c_valid,
    output wire c_ready,
    input wire [1:0] c_op,
    input wire [INST_W-1:0] c_instrument,
    input wire [TAG_W-1:0] c_tag,
    input wire [REV_W-1:0] c_expected_revision,
    input wire [SEQ_W-1:0] c_sequence,
    input wire c_sell,
    input wire [QTY_W-1:0] c_quantity,
    input wire signed [POS_W-1:0] c_position, c_floor, c_ceiling,
    input wire [QTY_W-1:0] c_bid_cap, c_ask_cap,
    input wire [POS_W-1:0] c_skew_trigger,
    input wire [PRICE_W-1:0] c_skew_ticks,

    output wire r_valid,
    input wire r_ready,
    output wire [1:0] r_op,
    output wire [INST_W-1:0] r_instrument,
    output wire [TAG_W-1:0] r_tag,
    output wire [3:0] r_status,
    output wire r_armed,
    output wire [REV_W-1:0] r_revision,
    output wire [SEQ_W-1:0] r_next_sequence,
    output wire signed [POS_W-1:0] r_position,
    output wire [31:0] r_fills, r_faults,

    input wire s_valid,
    output wire s_ready,
    input wire [INST_W-1:0] s_instrument,
    input wire s_active, s_current,
    input wire [EPOCH_W-1:0] s_epoch,
    input wire [PRICE_W-1:0] s_bid, s_ask,
    input wire [QTY_W-1:0] s_size,
    input wire [INSTRUMENTS-1:0] market_eligible,
    input wire [INSTRUMENTS*EPOCH_W-1:0] market_epoch,
    input wire [INSTRUMENTS*RESERVE_W-1:0] bid_reserved, ask_reserved,

    output wire m_valid,
    input wire m_ready,
    output wire m_current,
    output wire [INST_W-1:0] m_instrument,
    output wire [EPOCH_W-1:0] m_epoch,
    output wire [REV_W-1:0] m_revision,
    output wire [PRICE_W-1:0] m_bid, m_ask,
    output wire [QTY_W-1:0] m_bid_size, m_ask_size,
    output wire signed [POS_W-1:0] m_position,
    output wire [2:0] m_reason,
    output wire [INSTRUMENTS-1:0] bid_eligible, ask_eligible,
    output wire [INSTRUMENTS*QTY_W-1:0] live_bid_size, live_ask_size
);
    wire [INSTRUMENTS-1:0] armed_banks, changing_banks;
    wire [INSTRUMENTS*REV_W-1:0] revisions;
    wire [INSTRUMENTS*POLICY_W-1:0] policy;

    quote_inventory_ledger #(
        .INSTRUMENTS(INSTRUMENTS), .POS_W(POS_W), .QTY_W(QTY_W), .PRICE_W(PRICE_W),
        .SEQ_W(SEQ_W), .REV_W(REV_W), .TAG_W(TAG_W)
    ) ledger (.*);

    quote_inventory_overlay #(
        .INSTRUMENTS(INSTRUMENTS), .LEVELS(LEVELS), .POS_W(POS_W), .QTY_W(QTY_W), .RESERVE_W(RESERVE_W),
        .EPOCH_W(EPOCH_W), .REV_W(REV_W)
    ) overlay (.*);
endmodule

`default_nettype wire
