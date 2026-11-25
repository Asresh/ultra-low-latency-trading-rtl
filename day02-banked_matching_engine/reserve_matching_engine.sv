`timescale 1ns/1ps
`default_nettype none
`include "reserve_codes.svh"

// Banked entry point for reserve orders. The original matching top retains
// its smaller ABI and cycle contract.
module reserve_matching_engine #(
    parameter integer BANKS = 3,
    parameter integer PRICE_LEVELS = 12,
    parameter integer ORDER_SLOTS = 12,
    parameter integer QTY_W = 16,
    parameter integer OWNER_W = 8,
    parameter integer TAG_W = 16,
    parameter integer SEQ_W = 16,
    parameter integer CMD_DEPTH = 4,
    parameter integer EVENT_DEPTH = 4,
    localparam integer BANK_W = $clog2(BANKS + 1),
    localparam integer PRICE_W = $clog2(PRICE_LEVELS + 1),
    localparam integer ID_W = $clog2(ORDER_SLOTS + 1),
    localparam integer SUM_W = QTY_W + ID_W,
    localparam integer COMMAND_W = 8 + ID_W + PRICE_W + 3*QTY_W + OWNER_W,
    localparam integer RESULT_W = 11 + ID_W + PRICE_W + 5*QTY_W
                                + OWNER_W + 2*SUM_W + ID_W
) (
    input  wire                  clk,
    input  wire                  rst,
    input  wire                  s_valid,
    output wire                  s_ready,
    input  wire [BANK_W-1:0]     s_bank,
    input  wire [TAG_W-1:0]      s_tag,
    input  wire [COMMAND_W-1:0]  s_command,
    output wire                  m_valid,
    input  wire                  m_ready,
    output wire [BANK_W-1:0]     m_bank,
    output wire [TAG_W-1:0]      m_tag,
    output wire [SEQ_W-1:0]      m_command_sequence,
    output wire [SEQ_W-1:0]      m_event_sequence,
    output wire [ID_W-1:0]       m_order_id,
    output wire [RESULT_W-1:0]   m_result
);
    localparam integer HEADER_W = BANK_W + TAG_W + 2*SEQ_W + ID_W;
    localparam integer PACKET_W = HEADER_W + RESULT_W;
    localparam integer PORTS = BANKS + 1;
    localparam integer BANK_ADDR_W = BANKS > 1 ? $clog2(BANKS) : 1;
    localparam integer ID_LSB = PRICE_W + 3*QTY_W + OWNER_W;

    wire [BANKS-1:0] ready_in;
    wire [PORTS-1:0] lane_valid, lane_ready;
    wire [PORTS*PACKET_W-1:0] lane_payload;
    wire [PACKET_W-1:0] merged;
    wire bad_ready;
    wire valid_bank = s_bank < BANK_W'(BANKS);
    wire [ID_W-1:0] order_id = s_command[ID_LSB+:ID_W];
    logic [SEQ_W-1:0] invalid_sequence;
    wire [RESULT_W-1:0] invalid_result = {
        `RM_EV_REJECT, 1'b1, (RESULT_W-9)'(0), `RM_ERR_BANK
    };

    assign s_ready = valid_bank ? ready_in[BANK_ADDR_W'(s_bank)] : bad_ready;
    assign {m_bank, m_tag, m_command_sequence, m_event_sequence, m_order_id, m_result} = merged;

    for (genvar index = 0; index < BANKS; index = index + 1) begin : banks
        localparam logic [BANK_W-1:0] BANK = BANK_W'(index);
        localparam integer QUEUE_W = SEQ_W + TAG_W + COMMAND_W;
        logic [SEQ_W-1:0] next_command_sequence, next_event_sequence, active_sequence;
        logic [TAG_W-1:0] active_tag;
        logic [ID_W-1:0] active_order;
        wire cmd_valid, cmd_ready, result_valid, result_ready;
        wire [QUEUE_W-1:0] queued;
        wire [COMMAND_W-1:0] command = queued[0+:COMMAND_W];
        wire [RESULT_W-1:0] result;

        matching_fifo #(.WIDTH(QUEUE_W), .DEPTH(CMD_DEPTH)) input_fifo (
            .clk(clk), .rst(rst), .s_valid(s_valid && s_bank == BANK),
            .s_ready(ready_in[index]), .s_payload({next_command_sequence, s_tag, s_command}),
            .m_valid(cmd_valid), .m_ready(cmd_ready), .m_payload(queued)
        );

        reserve_order_book #(
            .PRICE_LEVELS(PRICE_LEVELS), .ORDER_SLOTS(ORDER_SLOTS),
            .QTY_W(QTY_W), .OWNER_W(OWNER_W)
        ) book (
            .clk(clk), .rst(rst), .s_valid(cmd_valid), .s_ready(cmd_ready),
            .s_command(command), .m_valid(result_valid), .m_ready(result_ready),
            .m_result(result)
        );

        matching_fifo #(.WIDTH(PACKET_W), .DEPTH(EVENT_DEPTH)) output_fifo (
            .clk(clk), .rst(rst), .s_valid(result_valid), .s_ready(result_ready),
            .s_payload({BANK, active_tag, active_sequence, next_event_sequence, active_order, result}),
            .m_valid(lane_valid[index]), .m_ready(lane_ready[index]),
            .m_payload(lane_payload[index*PACKET_W+:PACKET_W])
        );

        always_ff @(posedge clk) begin
            if (rst) begin
                next_command_sequence <= '0;
                next_event_sequence <= '0;
                active_tag <= '0;
                active_order <= '0;
                active_sequence <= '0;
            end else begin
                if (s_valid && s_ready && s_bank == BANK)
                    next_command_sequence <= next_command_sequence + 1'b1;
                if (cmd_valid && cmd_ready) begin
                    {active_sequence, active_tag} <= queued[COMMAND_W+:SEQ_W+TAG_W];
                    active_order <= command[ID_LSB+:ID_W];
                end
                if (result_valid && result_ready)
                    next_event_sequence <= next_event_sequence + 1'b1;
            end
        end
    end

    matching_fifo #(.WIDTH(PACKET_W), .DEPTH(EVENT_DEPTH)) invalid_bank_fifo (
        .clk(clk), .rst(rst), .s_valid(s_valid && !valid_bank), .s_ready(bad_ready),
        .s_payload({s_bank, s_tag, invalid_sequence, invalid_sequence, order_id, invalid_result}),
        .m_valid(lane_valid[BANKS]), .m_ready(lane_ready[BANKS]),
        .m_payload(lane_payload[BANKS*PACKET_W+:PACKET_W])
    );

    always_ff @(posedge clk) begin
        if (rst)
            invalid_sequence <= '0;
        else if (s_valid && s_ready && !valid_bank)
            invalid_sequence <= invalid_sequence + 1'b1;
    end

    matching_arbiter #(.PORTS(PORTS), .WIDTH(PACKET_W)) arbiter (
        .clk(clk), .rst(rst), .s_valid(lane_valid), .s_ready(lane_ready),
        .s_payload(lane_payload), .m_valid(m_valid), .m_ready(m_ready), .m_payload(merged)
    );
endmodule

`default_nettype wire
