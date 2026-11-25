`timescale 1ns/1ps
`include "quote_codes.svh"

// The store owns one replaceable quote per (instrument, venue) and fences
// duplicate or old updates. The read port exposes the pre-edge state.
module quote_store #(
    parameter integer INSTRUMENTS = 4,
    parameter integer VENUES = 4,
    parameter integer PRICE_W = 20,
    parameter integer QTY_W = 14,
    parameter integer TIME_W = 16,
    parameter integer SEQ_W = 12,
    parameter integer TAG_W = 16,
    parameter integer INST_W = INSTRUMENTS > 1 ? $clog2(INSTRUMENTS + 1) : 1,
    parameter integer VENUE_W = VENUES > 1 ? $clog2(VENUES + 1) : 1,
    localparam integer RECORD_W = 2 * (PRICE_W + QTY_W) + TIME_W + SEQ_W
)(
    input  wire                      clk,
    input  wire                      rst,
    input  wire  [TIME_W-1:0]        now_tick,
    input  wire                      cmd_valid,
    output wire                      cmd_ready,
    input  wire  [2:0]               cmd_op,
    input  wire  [INST_W-1:0]        cmd_instrument,
    input  wire  [VENUE_W-1:0]       cmd_venue,
    input  wire  [SEQ_W-1:0]         cmd_sequence,
    input  wire  [PRICE_W-1:0]       cmd_bid_price,
    input  wire  [PRICE_W-1:0]       cmd_ask_price,
    input  wire  [QTY_W-1:0]         cmd_bid_qty,
    input  wire  [QTY_W-1:0]         cmd_ask_qty,
    input  wire  [TAG_W-1:0]         cmd_tag,
    output logic                     ack_valid,
    input  wire                      ack_ready,
    output logic [3:0]               ack_code,
    output logic [TAG_W-1:0]         ack_tag,
    input  wire  [INST_W-1:0]        read_instrument,
    output wire  [VENUES-1:0]        read_enabled,
    output wire  [VENUES-1:0]        read_present,
    output wire  [VENUES*RECORD_W-1:0] read_records
);
    /* verilator lint_off UNUSEDSIGNAL */
    wire unused_mutation;
    /* verilator lint_on UNUSEDSIGNAL */
    quote_store_engine #(
        .INSTRUMENTS(INSTRUMENTS), .VENUES(VENUES), .PRICE_W(PRICE_W), .QTY_W(QTY_W),
        .TIME_W(TIME_W), .SEQ_W(SEQ_W), .TAG_W(TAG_W), .INST_W(INST_W), .VENUE_W(VENUE_W)
    ) core (.mutation_commit(unused_mutation), .*);
endmodule
