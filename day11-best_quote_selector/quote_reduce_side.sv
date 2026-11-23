`timescale 1ns/1ps

// Rank parallel venue candidates in a padded binary tree. At equal prices the
// left subtree always wins, which preserves the lowest venue index.
module quote_reduce_side #(
    parameter integer VENUES = 4,
    parameter integer PRICE_W = 20,
    parameter integer PAYLOAD_W = 42,
    parameter integer VENUE_W = VENUES > 1 ? $clog2(VENUES + 1) : 1,
    parameter bit HIGH_WINS = 1'b1,
    localparam integer LEAVES = 2 ** $clog2(VENUES)
)(
    input  wire [VENUES-1:0]              candidate_valid,
    input  wire [VENUES*PRICE_W-1:0]      candidate_price,
    input  wire [VENUES*PAYLOAD_W-1:0]    candidate_payload,
    output wire                           winner_valid,
    output wire [PRICE_W-1:0]            winner_price,
    output wire [PAYLOAD_W-1:0]          winner_payload,
    output wire [VENUE_W-1:0]            winner_venue
);
    /* verilator lint_off UNOPTFLAT */
    wire valid [1:2*LEAVES-1];
    wire [PRICE_W-1:0] price [1:2*LEAVES-1];
    wire [PAYLOAD_W-1:0] payload [1:2*LEAVES-1];
    wire [VENUE_W-1:0] venue [1:2*LEAVES-1];
    /* verilator lint_on UNOPTFLAT */

    for (genvar i = 0; i < LEAVES; i++) begin : g_leaves
        if (i < VENUES) begin : g_live
            assign valid[LEAVES+i] = candidate_valid[i];
            assign price[LEAVES+i] = candidate_price[i*PRICE_W +: PRICE_W];
            assign payload[LEAVES+i] = candidate_payload[i*PAYLOAD_W +: PAYLOAD_W];
            assign venue[LEAVES+i] = VENUE_W'(i);
        end else begin : g_padding
            assign valid[LEAVES+i] = 1'b0;
            assign price[LEAVES+i] = '0;
            assign payload[LEAVES+i] = '0;
            assign venue[LEAVES+i] = '0;
        end
    end

    for (genvar i = 1; i < LEAVES; i++) begin : g_parents
        localparam integer LEFT = 2*i;
        localparam integer RIGHT = LEFT+1;
        wire better_or_equal = HIGH_WINS ?
            price[LEFT] >= price[RIGHT] : price[LEFT] <= price[RIGHT];
        wire take_left = !valid[RIGHT] || (valid[LEFT] && better_or_equal);
        assign valid[i] = valid[LEFT] || valid[RIGHT];
        assign price[i] = take_left ? price[LEFT] : price[RIGHT];
        assign payload[i] = take_left ? payload[LEFT] : payload[RIGHT];
        assign venue[i] = take_left ? venue[LEFT] : venue[RIGHT];
    end

    assign winner_valid = valid[1];
    assign winner_price = valid[1] ? price[1] : '0;
    assign winner_payload = valid[1] ? payload[1] : '0;
    assign winner_venue = valid[1] ? venue[1] : '0;
endmodule
