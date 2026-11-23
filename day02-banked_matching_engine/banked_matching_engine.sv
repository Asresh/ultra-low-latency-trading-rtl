`timescale 1ns/1ps
`default_nettype none
`include "matching_codes.svh"

module banked_matching_engine #(
    parameter integer BANKS = 3,
    parameter integer PRICE_LEVELS = 12,
    parameter integer ORDER_SLOTS = 12,
    parameter integer QTY_W = 16,
    parameter integer TAG_W = 16,
    parameter integer SEQ_W = 16,
    parameter integer CMD_DEPTH = 4,
    parameter integer EVENT_DEPTH = 4,
    localparam integer BANK_W = $clog2(BANKS + 1),
    localparam integer PRICE_W = $clog2(PRICE_LEVELS + 1),
    localparam integer ID_W = $clog2(ORDER_SLOTS + 1),
    localparam integer LEVEL_QTY_W = QTY_W + ID_W
) (
    input wire clk,
    input wire rst,
    input wire s_valid,
    output wire s_ready,
    input wire [BANK_W-1:0] s_instrument,
    input wire [1:0] s_opcode,
    input wire [1:0] s_tif,
    input wire s_is_ask,
    input wire [ID_W-1:0] s_order_id,
    input wire [PRICE_W-1:0] s_price,
    input wire [QTY_W-1:0] s_qty,
    input wire [TAG_W-1:0] s_tag,
    output wire m_valid,
    input wire m_ready,
    output wire [BANK_W-1:0] m_instrument,
    output wire [2:0] m_kind,
    output wire m_last,
    output wire [ID_W-1:0] m_order_id,
    output wire m_is_ask,
    output wire [ID_W-1:0] m_maker_id,
    output wire [PRICE_W-1:0] m_price,
    output wire [QTY_W-1:0] m_qty,
    output wire [QTY_W-1:0] m_remaining,
    output wire [3:0] m_reject,
    output wire [LEVEL_QTY_W-1:0] m_level_qty,
    output wire [ID_W-1:0] m_level_count,
    output wire [TAG_W-1:0] m_tag,
    output wire [SEQ_W-1:0] m_cmd_seq,
    output wire [SEQ_W-1:0] m_event_seq
);
    localparam integer BANK_ADDR_W = (BANKS > 1) ? $clog2(BANKS) : 1;
    localparam integer COMMAND_W = 2 + 2 + 1 + ID_W + PRICE_W + QTY_W + TAG_W;
    localparam integer RESULT_W = 3 + 1 + ID_W + 1 + ID_W + PRICE_W + 2*QTY_W + 4 + LEVEL_QTY_W + ID_W;
    localparam integer EVENT_W = TAG_W + 2*SEQ_W + RESULT_W;
    localparam integer PACKET_W = BANK_W + EVENT_W;
    localparam integer PORTS = BANKS + 1;

    wire [COMMAND_W-1:0] command_in = {
        s_tag, s_opcode, s_tif, s_is_ask, s_order_id, s_price, s_qty
    };
    wire bank_exists = s_instrument < BANK_W'(BANKS);
    wire [BANKS-1:0] lane_s_ready;
    wire [PORTS-1:0] lane_m_valid, lane_m_ready;
    wire [PORTS*PACKET_W-1:0] lane_m_packet;
    wire [PACKET_W-1:0] output_packet;
    wire bad_ready;

    assign s_ready = bank_exists ? lane_s_ready[BANK_ADDR_W'(s_instrument)] : bad_ready;
    assign {m_instrument, m_tag, m_cmd_seq, m_event_seq, m_kind, m_last, m_order_id,
            m_is_ask, m_maker_id, m_price, m_qty, m_remaining, m_reject, m_level_qty,
            m_level_count} = output_packet;

    for (genvar b = 0; b < BANKS; b++) begin : gen_bank
        localparam logic [BANK_W-1:0] BANK_CODE = BANK_W'(b);
        wire [EVENT_W-1:0] event_out;
        matching_bank #(
            .PRICE_LEVELS(PRICE_LEVELS), .ORDER_SLOTS(ORDER_SLOTS), .QTY_W(QTY_W),
            .TAG_W(TAG_W), .SEQ_W(SEQ_W), .CMD_DEPTH(CMD_DEPTH), .EVENT_DEPTH(EVENT_DEPTH)
        ) bank (
            .clk(clk), .rst(rst), .s_valid(s_valid && s_instrument == BANK_CODE),
            .s_ready(lane_s_ready[b]), .s_command(command_in),
            .m_valid(lane_m_valid[b]), .m_ready(lane_m_ready[b]), .m_event(event_out)
        );
        assign lane_m_packet[b*PACKET_W+:PACKET_W] = {BANK_CODE, event_out};
    end

    logic [SEQ_W-1:0] bad_seq;
    wire [RESULT_W-1:0] bad_result = {
        `EV_REJECT, 1'b1, s_order_id, 1'b0, ID_W'(0), PRICE_W'(0),
        QTY_W'(0), QTY_W'(0), `ERR_BANK, LEVEL_QTY_W'(0), ID_W'(0)
    };
    matching_fifo #(.WIDTH(PACKET_W), .DEPTH(EVENT_DEPTH)) invalid_instrument (
        .clk(clk), .rst(rst), .s_valid(s_valid && !bank_exists), .s_ready(bad_ready),
        .s_payload({s_instrument, s_tag, bad_seq, bad_seq, bad_result}),
        .m_valid(lane_m_valid[BANKS]), .m_ready(lane_m_ready[BANKS]),
        .m_payload(lane_m_packet[BANKS*PACKET_W+:PACKET_W])
    );

    always_ff @(posedge clk) begin
        if (rst)
            bad_seq <= '0;
        else if (s_valid && s_ready && !bank_exists)
            bad_seq <= bad_seq + SEQ_W'(1);
    end

    matching_arbiter #(.PORTS(PORTS), .WIDTH(PACKET_W)) event_arbiter (
        .clk(clk), .rst(rst), .s_valid(lane_m_valid), .s_ready(lane_m_ready),
        .s_payload(lane_m_packet), .m_valid(m_valid), .m_ready(m_ready), .m_payload(output_packet)
    );
endmodule

`default_nettype wire
