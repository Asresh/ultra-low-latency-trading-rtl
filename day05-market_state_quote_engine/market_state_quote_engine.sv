`timescale 1ns/1ps
`default_nettype none
`include "market_codes.svh"

module market_state_quote_engine #(
    parameter integer BANKS = 2,
    parameter integer LEVELS = 16,
    parameter integer SLOTS = 16,
    parameter integer QTY_W = 12,
    parameter integer SEQ_W = 16,
    parameter integer EPOCH_W = 8,
    parameter integer TAG_W = 16,
    parameter integer STAT_W = 32,
    parameter integer REV_W = 16,
    parameter integer MAX_IDLE_TICKS = 16,
    parameter integer QUOTE_SIZE = 12,
    parameter integer HALF_SPREAD = 1,
    parameter integer SKEW_TICKS = 1,
    parameter integer MIN_MARKET_SPREAD = 2,
    parameter integer IMBALANCE_Q8 = 64,
    localparam integer BANK_W = $clog2(BANKS + 1),
    localparam integer PRICE_W = $clog2(LEVELS + 1),
    localparam integer ID_W = $clog2(SLOTS + 1),
    localparam integer DEPTH_W = QTY_W + ID_W
) (
    input wire clk,
    input wire rst,
    input wire tick,
    input wire s_valid,
    output wire s_ready,
    input wire [BANK_W-1:0] s_instrument,
    input wire [3:0] s_op,
    input wire [SEQ_W-1:0] s_seq,
    input wire [ID_W-1:0] s_id,
    input wire [ID_W-1:0] s_new_id,
    input wire s_side,
    input wire [PRICE_W-1:0] s_price,
    input wire [QTY_W-1:0] s_qty,
    input wire [3:0] s_stat_index,
    input wire [TAG_W-1:0] s_tag,
    output wire r_valid,
    input wire r_ready,
    output wire [BANK_W-1:0] r_instrument,
    output wire [3:0] r_op,
    output wire [SEQ_W-1:0] r_seq,
    output wire [ID_W-1:0] r_id,
    output wire [TAG_W-1:0] r_tag,
    output wire [3:0] r_status,
    output wire [1:0] r_mode,
    output wire [SEQ_W-1:0] r_next_seq,
    output wire [EPOCH_W-1:0] r_epoch,
    output wire r_present,
    output wire r_side,
    output wire [PRICE_W-1:0] r_price,
    output wire [DEPTH_W-1:0] r_qty,
    output wire [ID_W-1:0] r_count,
    output wire [STAT_W-1:0] r_stat,
    output wire r_bid_present,
    output wire r_ask_present,
    output wire [PRICE_W-1:0] r_bid,
    output wire [PRICE_W-1:0] r_ask,
    output wire [DEPTH_W-1:0] r_bid_qty,
    output wire [DEPTH_W-1:0] r_ask_qty,
    output wire [ID_W-1:0] r_bid_count,
    output wire [ID_W-1:0] r_ask_count,
    output wire q_valid,
    input wire q_ready,
    output wire [BANK_W-1:0] q_instrument,
    output wire q_active,
    output wire [EPOCH_W-1:0] q_epoch,
    output wire [REV_W-1:0] q_revision,
    output wire [PRICE_W-1:0] q_bid,
    output wire [PRICE_W-1:0] q_ask,
    output wire [QTY_W-1:0] q_size,
    output wire [2:0] q_reason,
    output wire q_current,
    output wire [BANKS-1:0] o_live,
    output wire [BANKS-1:0] o_eligible,
    output wire [BANKS-1:0] o_up_pressure,
    output wire [BANKS-1:0] o_down_pressure,
    output wire [BANKS*EPOCH_W-1:0] o_epoch
);
    localparam integer BANK_ADDR_W = (BANKS > 1) ? $clog2(BANKS) : 1;
    localparam integer QUOTE_KEY_W = 1+EPOCH_W+2*PRICE_W+QTY_W;
    localparam integer RESP_W = 4+2+SEQ_W+EPOCH_W+2+PRICE_W+DEPTH_W+ID_W+STAT_W
                                + 2+2*PRICE_W+2*DEPTH_W+2*ID_W;
    localparam integer ECHO_W = BANK_W+4+SEQ_W+ID_W+TAG_W;

    logic [ECHO_W+RESP_W-1:0] response_output;
    logic response_full;
    wire [RESP_W-1:0] response_by_bank [0:BANKS-1];
    wire [BANKS*QUOTE_KEY_W-1:0] desired;
    wire [BANKS*3-1:0] reasons;
    wire exists = s_instrument < BANK_W'(BANKS);

    assign s_ready = !rst && (!response_full || r_ready);
    assign r_valid = response_full && !rst;
    wire consume = s_valid && s_ready;

    assign {r_instrument, r_op, r_seq, r_id, r_tag, r_status, r_mode, r_next_seq, r_epoch,
            r_present, r_side, r_price, r_qty, r_count, r_stat, r_bid_present, r_ask_present,
            r_bid, r_ask, r_bid_qty, r_ask_qty, r_bid_count, r_ask_count} = response_output;

    for (genvar bank = 0; bank < BANKS; bank++) begin : bank_i
        wire [EPOCH_W-1:0] epoch, response_epoch;
        wire [3:0] status;
        wire [1:0] mode;
        wire [SEQ_W-1:0] expected;
        wire present, side;
        wire [PRICE_W-1:0] price;
        wire [DEPTH_W-1:0] qty;
        wire [ID_W-1:0] count;
        wire [STAT_W-1:0] stat;
        wire bid_present, ask_present;
        wire [PRICE_W-1:0] bid, ask;
        wire [DEPTH_W-1:0] bid_qty, ask_qty;
        wire [ID_W-1:0] bid_count, ask_count;
        wire [PRICE_W-1:0] quote_bid, quote_ask;
        wire [QTY_W-1:0] sized_order;
        wire [2:0] reason;
        wire report_bbo = s_op == `MOP_BBO;

        assign o_epoch[bank*EPOCH_W+:EPOCH_W] = epoch;
        assign response_by_bank[bank] = {
            status, mode, expected, response_epoch, present, side, price, qty, count, stat,
            (report_bbo && bid_present), (report_bbo && ask_present),
            (report_bbo ? bid : PRICE_W'(0)), (report_bbo ? ask : PRICE_W'(0)),
            (report_bbo ? bid_qty : DEPTH_W'(0)), (report_bbo ? ask_qty : DEPTH_W'(0)),
            (report_bbo ? bid_count : ID_W'(0)), (report_bbo ? ask_count : ID_W'(0))
        };
        assign desired[bank*QUOTE_KEY_W+:QUOTE_KEY_W] = {
            o_eligible[bank], epoch, quote_bid, quote_ask, sized_order
        };
        assign reasons[bank*3+:3] = reason;

        market_instrument #(
            .LEVELS(LEVELS), .SLOTS(SLOTS), .QTY_W(QTY_W), .SEQ_W(SEQ_W),
            .EPOCH_W(EPOCH_W), .STAT_W(STAT_W), .MAX_IDLE_TICKS(MAX_IDLE_TICKS)
        ) instrument (
            .clk(clk), .rst(rst), .tick(tick), .accept(consume && s_instrument == BANK_W'(bank)),
            .op(s_op), .sequence_in(s_seq), .id(s_id), .replacement(s_new_id), .side(s_side),
            .price(s_price), .quantity(s_qty), .stat_index(s_stat_index),
            .live(o_live[bank]), .epoch(epoch), .rsp_status(status), .rsp_mode(mode),
            .rsp_expected(expected), .rsp_epoch(response_epoch), .rsp_present(present),
            .rsp_side(side), .rsp_price(price), .rsp_qty(qty), .rsp_count(count), .rsp_stat(stat),
            .bid_present(bid_present), .ask_present(ask_present), .bid_price(bid), .ask_price(ask),
            .bid_qty(bid_qty), .ask_qty(ask_qty), .bid_count(bid_count), .ask_count(ask_count)
        );

        passive_quote_policy #(
            .LEVELS(LEVELS), .QTY_W(QTY_W), .DEPTH_W(DEPTH_W), .BASE_QTY(QUOTE_SIZE),
            .HALF_SPREAD(HALF_SPREAD), .SKEW_TICKS(SKEW_TICKS),
            .MIN_MARKET_SPREAD(MIN_MARKET_SPREAD), .IMBALANCE_Q8(IMBALANCE_Q8)
        ) policy (
            .live(o_live[bank]), .bid_present(bid_present), .ask_present(ask_present),
            .bid_price(bid), .ask_price(ask), .bid_size(bid_qty), .ask_size(ask_qty),
            .active(o_eligible[bank]), .quote_bid(quote_bid), .quote_ask(quote_ask),
            .quote_size(sized_order), .reason(reason),
            .pressure_bid(o_up_pressure[bank]), .pressure_ask(o_down_pressure[bank])
        );
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            response_full <= 1'b0;
            response_output <= '0;
        end else if (s_ready) begin
            response_full <= s_valid;
            if (s_valid) begin
                response_output <= {
                    s_instrument, s_op, s_seq, s_id, s_tag,
                    (exists ? response_by_bank[BANK_ADDR_W'(s_instrument)] :
                              {`ME_BANK, {(RESP_W-4){1'b0}}})
                };
            end
        end
    end

    quote_publication_pipeline #(
        .BANKS(BANKS), .PRICE_W(PRICE_W), .QTY_W(QTY_W), .EPOCH_W(EPOCH_W), .REV_W(REV_W)
    ) publisher (
        .clk(clk), .rst(rst), .desired_now(desired), .reason_now(reasons),
        .q_valid(q_valid), .q_ready(q_ready), .q_instrument(q_instrument), .q_active(q_active),
        .q_epoch(q_epoch), .q_revision(q_revision), .q_bid(q_bid), .q_ask(q_ask), .q_size(q_size),
        .q_reason(q_reason), .q_current(q_current)
    );
endmodule

`default_nettype wire
