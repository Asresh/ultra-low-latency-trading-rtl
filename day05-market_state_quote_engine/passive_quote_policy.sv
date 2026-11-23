`timescale 1ns/1ps
`default_nettype none
`include "market_codes.svh"

module passive_quote_policy #(
    parameter integer LEVELS = 16,
    parameter integer QTY_W = 12,
    parameter integer DEPTH_W = 17,
    parameter integer BASE_QTY = 12,
    parameter integer HALF_SPREAD = 1,
    parameter integer SKEW_TICKS = 1,
    parameter integer MIN_MARKET_SPREAD = 1,
    parameter integer IMBALANCE_Q8 = 64,
    localparam integer PRICE_W = $clog2(LEVELS + 1)
) (
    input wire live,
    input wire bid_present,
    input wire ask_present,
    input wire [PRICE_W-1:0] bid_price,
    input wire [PRICE_W-1:0] ask_price,
    input wire [DEPTH_W-1:0] bid_size,
    input wire [DEPTH_W-1:0] ask_size,
    output logic active,
    output logic [PRICE_W-1:0] quote_bid,
    output logic [PRICE_W-1:0] quote_ask,
    output logic [QTY_W-1:0] quote_size,
    output logic [2:0] reason,
    output wire pressure_bid,
    output wire pressure_ask
);
    localparam integer CALC_W = PRICE_W+4;
    localparam logic [DEPTH_W-1:0] SIZE_CAP = DEPTH_W'(BASE_QTY);

    // Move both depth terms to opposite sides of the inequality. The
    // coefficients are static and commonly share a power-of-two divisor.
    function automatic integer divisor(input integer a, input integer b);
        integer x, y, rem;
        x = a;
        y = b;
        while (y != 0) begin
            rem = x % y;
            x = y;
            y = rem;
        end
        divisor = x;
    endfunction

    localparam integer SCALE_LO = 256-IMBALANCE_Q8;
    localparam integer SCALE_HI = 256+IMBALANCE_Q8;
    localparam integer COMMON = divisor(SCALE_LO, SCALE_HI);
    localparam integer SMALLER_WEIGHT = SCALE_LO/COMMON;
    localparam integer LARGER_WEIGHT = SCALE_HI/COMMON;
    localparam integer IMB_W = DEPTH_W+$clog2(LARGER_WEIGHT+1);

    wire [DEPTH_W-1:0] thin_depth = (bid_size < ask_size) ? bid_size : ask_size;
    wire [DEPTH_W-1:0] reduced_size = (thin_depth < SIZE_CAP) ? thin_depth : SIZE_CAP;
    wire bid_stronger = bid_size > ask_size;
    wire ask_stronger = ask_size > bid_size;
    wire [IMB_W-1:0] bid_smaller = IMB_W'(bid_size) * IMB_W'(SMALLER_WEIGHT);
    wire [IMB_W-1:0] ask_smaller = IMB_W'(ask_size) * IMB_W'(SMALLER_WEIGHT);
    wire [IMB_W-1:0] bid_larger = IMB_W'(bid_size) * IMB_W'(LARGER_WEIGHT);
    wire [IMB_W-1:0] ask_larger = IMB_W'(ask_size) * IMB_W'(LARGER_WEIGHT);

    assign pressure_bid = live && bid_present && ask_present && bid_stronger && bid_smaller >= ask_larger;
    assign pressure_ask = live && bid_present && ask_present && ask_stronger && ask_smaller >= bid_larger;

    wire signed [CALC_W-1:0] signed_bid = $signed(CALC_W'(bid_price));
    wire signed [CALC_W-1:0] signed_ask = $signed(CALC_W'(ask_price));
    wire signed [CALC_W-1:0] sum_price = signed_bid + signed_ask;
    wire signed [CALC_W-1:0] bid_mid = sum_price >>> 1;
    wire signed [CALC_W-1:0] ask_mid = (sum_price + CALC_W'(1)) >>> 1;
    wire signed [CALC_W-1:0] adjustment = pressure_bid ? CALC_W'(SKEW_TICKS)
        : (pressure_ask ? -CALC_W'(SKEW_TICKS) : CALC_W'(0));
    wire signed [CALC_W-1:0] raw_bid = bid_mid + adjustment - CALC_W'(HALF_SPREAD);
    wire signed [CALC_W-1:0] raw_ask = ask_mid + adjustment + CALC_W'(HALF_SPREAD);
    wire signed [CALC_W-1:0] passive_ceiling = signed_ask - CALC_W'(1);
    wire signed [CALC_W-1:0] passive_floor = signed_bid + CALC_W'(1);

    // Convert a scaled fixed-point price to the configured tick-price grid.
    function automatic signed [CALC_W-1:0] to_grid(input signed [CALC_W-1:0] price);
        if (price < 0)
            to_grid = '0;
        else if (price > CALC_W'(LEVELS-1))
            to_grid = CALC_W'(LEVELS-1);
        else
            to_grid = price;
    endfunction

    wire signed [CALC_W-1:0] grid_bid = to_grid(raw_bid);
    wire signed [CALC_W-1:0] grid_ask = to_grid(raw_ask);
    wire signed [CALC_W-1:0] passive_bid = grid_bid < passive_ceiling ? grid_bid : passive_ceiling;
    wire signed [CALC_W-1:0] passive_ask = grid_ask > passive_floor ? grid_ask : passive_floor;
    wire signed [CALC_W-1:0] observed_spread = signed_ask - signed_bid;

    always_comb begin
        active = 1'b0;
        quote_bid = '0;
        quote_ask = '0;
        quote_size = '0;
        if (!live)
            reason = `QR_INACTIVE;
        else if (!bid_present || !ask_present)
            reason = `QR_NO_SIDES;
        else if (bid_price >= ask_price)
            reason = `QR_CROSSED;
        else if (observed_spread < CALC_W'(MIN_MARKET_SPREAD) || passive_bid >= passive_ask)
            reason = `QR_SPREAD;
        else if (reduced_size == '0)
            reason = `QR_ZERO_SIZE;
        else begin
            reason = `QR_QUOTING;
            active = 1'b1;
            quote_bid = PRICE_W'(passive_bid);
            quote_ask = PRICE_W'(passive_ask);
            quote_size = QTY_W'(reduced_size);
        end
    end
endmodule

`default_nettype wire
