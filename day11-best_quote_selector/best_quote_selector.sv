`timescale 1ns/1ps

// A command and a query may arrive together. The query samples the state
// before that edge, so a simultaneous command becomes visible one edge later.
module best_quote_selector #(
    parameter integer INSTRUMENTS = 4,
    parameter integer VENUES = 4,
    parameter integer PRICE_W = 20,
    parameter integer QTY_W = 14,
    parameter integer TIME_W = 16,
    parameter integer SEQ_W = 12,
    parameter integer TAG_W = 16,
    localparam integer INST_W = INSTRUMENTS > 1 ? $clog2(INSTRUMENTS + 1) : 1,
    localparam integer VENUE_W = VENUES > 1 ? $clog2(VENUES + 1) : 1,
    localparam integer RECORD_W = 2 * (PRICE_W + QTY_W) + TIME_W + SEQ_W
)(
    input wire                      clk, rst,
    input wire [TIME_W-1:0]         now_tick,
    input wire                      cmd_valid,
    output wire                     cmd_ready,
    input wire [2:0]                cmd_op,
    input wire [INST_W-1:0]         cmd_instrument,
    input wire [VENUE_W-1:0]        cmd_venue,
    input wire [SEQ_W-1:0]          cmd_sequence,
    input wire [PRICE_W-1:0]        cmd_bid_price, cmd_ask_price,
    input wire [QTY_W-1:0]          cmd_bid_qty, cmd_ask_qty,
    input wire [TAG_W-1:0]          cmd_tag,
    output wire                     ack_valid,
    input wire                      ack_ready,
    output wire [3:0]               ack_code,
    output wire [TAG_W-1:0]         ack_tag,
    input wire                      query_valid,
    output wire                     query_ready,
    input wire [INST_W-1:0]         query_instrument,
    input wire [TIME_W-1:0]         query_max_age,
    input wire [TAG_W-1:0]          query_tag,
    output wire                     result_valid,
    input wire                      result_ready,
    output wire [TAG_W-1:0]         result_tag,
    output wire [3:0]               result_code,
    output wire [2:0]               result_state,
    output wire [TIME_W-1:0]        result_tick,
    output wire [VENUES-1:0]        result_active, result_stale,
    output wire                     result_bid_valid, result_ask_valid,
    output wire [VENUE_W-1:0]       result_bid_venue, result_ask_venue,
    output wire [PRICE_W-1:0]       result_bid_price, result_ask_price,
    output wire [QTY_W-1:0]         result_bid_qty, result_ask_qty,
    output wire [TIME_W-1:0]        result_bid_age, result_ask_age,
    output wire [SEQ_W-1:0]         result_bid_sequence, result_ask_sequence,
    output wire signed [PRICE_W:0]  result_spread
);
    wire [VENUES-1:0] read_enabled, read_present;
    wire [VENUES*RECORD_W-1:0] read_records;
    quote_store #(.INSTRUMENTS(INSTRUMENTS), .VENUES(VENUES), .PRICE_W(PRICE_W),
                  .QTY_W(QTY_W), .TIME_W(TIME_W), .SEQ_W(SEQ_W), .TAG_W(TAG_W),
                  .INST_W(INST_W), .VENUE_W(VENUE_W)) store (
        .clk(clk), .rst(rst), .now_tick(now_tick), .cmd_valid(cmd_valid),
        .cmd_ready(cmd_ready), .cmd_op(cmd_op), .cmd_instrument(cmd_instrument),
        .cmd_venue(cmd_venue), .cmd_sequence(cmd_sequence), .cmd_bid_price(cmd_bid_price),
        .cmd_ask_price(cmd_ask_price), .cmd_bid_qty(cmd_bid_qty), .cmd_ask_qty(cmd_ask_qty),
        .cmd_tag(cmd_tag), .ack_valid(ack_valid), .ack_ready(ack_ready),
        .ack_code(ack_code), .ack_tag(ack_tag), .read_instrument(query_instrument),
        .read_enabled(read_enabled), .read_present(read_present), .read_records(read_records)
    );
    quote_bbo #(.INSTRUMENTS(INSTRUMENTS), .VENUES(VENUES), .PRICE_W(PRICE_W),
                .QTY_W(QTY_W), .TIME_W(TIME_W), .SEQ_W(SEQ_W), .TAG_W(TAG_W),
                .INST_W(INST_W), .VENUE_W(VENUE_W)) selection (.*);
endmodule
