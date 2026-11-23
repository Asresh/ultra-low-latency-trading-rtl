`timescale 1ns/1ps
`default_nettype none

module matching_bank #(
    parameter integer PRICE_LEVELS = 12,
    parameter integer ORDER_SLOTS = 12,
    parameter integer QTY_W = 16,
    parameter integer TAG_W = 16,
    parameter integer SEQ_W = 16,
    parameter integer CMD_DEPTH = 4,
    parameter integer EVENT_DEPTH = 4,
    localparam integer PRICE_W = $clog2(PRICE_LEVELS + 1),
    localparam integer ID_W = $clog2(ORDER_SLOTS + 1),
    localparam integer LEVEL_QTY_W = QTY_W + ID_W,
    localparam integer COMMAND_W = 2 + 2 + 1 + ID_W + PRICE_W + QTY_W + TAG_W,
    localparam integer RESULT_W = 3 + 1 + ID_W + 1 + ID_W + PRICE_W + 2*QTY_W + 4 + LEVEL_QTY_W + ID_W,
    localparam integer EVENT_W = TAG_W + 2*SEQ_W + RESULT_W
) (
    input wire clk,
    input wire rst,
    input wire s_valid,
    output wire s_ready,
    input wire [COMMAND_W-1:0] s_command,
    output wire m_valid,
    input wire m_ready,
    output wire [EVENT_W-1:0] m_event
);
    logic [SEQ_W-1:0] input_seq;
    logic [SEQ_W-1:0] event_seq;
    logic [SEQ_W-1:0] active_seq;
    logic [TAG_W-1:0] active_tag;
    wire [SEQ_W+COMMAND_W-1:0] queued_command;
    wire cmd_valid, cmd_ready;
    wire book_valid, book_ready;

    wire [SEQ_W-1:0] command_seq;
    wire [TAG_W-1:0] command_tag;
    wire [1:0] opcode, tif;
    wire is_ask;
    wire [ID_W-1:0] id;
    wire [PRICE_W-1:0] price;
    wire [QTY_W-1:0] qty;
    assign {command_seq, command_tag, opcode, tif, is_ask, id, price, qty} = queued_command;

    wire [2:0] event_kind;
    wire event_last, event_side;
    wire [ID_W-1:0] event_id, event_maker, event_level_count;
    wire [PRICE_W-1:0] event_price;
    wire [QTY_W-1:0] event_qty, event_remaining;
    wire [3:0] event_reject;
    wire [LEVEL_QTY_W-1:0] event_level_qty;
    wire [RESULT_W-1:0] book_event = {
        event_kind, event_last, event_id, event_side, event_maker, event_price,
        event_qty, event_remaining, event_reject, event_level_qty, event_level_count
    };

    matching_fifo #(.WIDTH(SEQ_W+COMMAND_W), .DEPTH(CMD_DEPTH)) command_fifo (
        .clk(clk), .rst(rst), .s_valid(s_valid), .s_ready(s_ready),
        .s_payload({input_seq, s_command}), .m_valid(cmd_valid), .m_ready(cmd_ready),
        .m_payload(queued_command)
    );

    matching_book #(.PRICE_LEVELS(PRICE_LEVELS), .ORDER_SLOTS(ORDER_SLOTS), .QTY_W(QTY_W)) book (
        .clk(clk), .rst(rst), .s_valid(cmd_valid), .s_ready(cmd_ready),
        .s_opcode(opcode), .s_tif(tif), .s_is_ask(is_ask), .s_order_id(id), .s_price(price), .s_qty(qty),
        .m_valid(book_valid), .m_ready(book_ready), .m_kind(event_kind), .m_last(event_last),
        .m_order_id(event_id), .m_is_ask(event_side), .m_maker_id(event_maker),
        .m_price(event_price), .m_qty(event_qty), .m_remaining(event_remaining),
        .m_reject(event_reject), .m_level_qty(event_level_qty), .m_level_count(event_level_count)
    );

    matching_fifo #(.WIDTH(EVENT_W), .DEPTH(EVENT_DEPTH)) event_fifo (
        .clk(clk), .rst(rst), .s_valid(book_valid), .s_ready(book_ready),
        .s_payload({active_tag, active_seq, event_seq, book_event}), .m_valid(m_valid),
        .m_ready(m_ready), .m_payload(m_event)
    );

    always_ff @(posedge clk) begin
        if (rst) begin
            input_seq <= '0;
            event_seq <= '0;
            active_seq <= '0;
            active_tag <= '0;
        end else begin
            if (s_valid && s_ready)
                input_seq <= input_seq + SEQ_W'(1);
            if (cmd_valid && cmd_ready) begin
                active_seq <= command_seq;
                active_tag <= command_tag;
            end
            if (book_valid && book_ready)
                event_seq <= event_seq + SEQ_W'(1);
        end
    end
endmodule

`default_nettype wire
