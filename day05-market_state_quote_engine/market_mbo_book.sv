`timescale 1ns/1ps
`default_nettype none
`include "market_codes.svh"

module market_mbo_book #(
    parameter integer LEVELS = 16,
    parameter integer SLOTS = 16,
    parameter integer QTY_W = 12,
    localparam integer PRICE_W = $clog2(LEVELS + 1),
    localparam integer ID_W = $clog2(SLOTS + 1),
    localparam integer DEPTH_W = QTY_W + ID_W
) (
    input wire clk,
    input wire rst,
    input wire clear_book,
    input wire commit,
    input wire [3:0] op,
    input wire [ID_W-1:0] id,
    input wire [ID_W-1:0] replacement,
    input wire side,
    input wire [PRICE_W-1:0] price,
    input wire [QTY_W-1:0] quantity,
    output logic [3:0] error_code,
    output wire order_present,
    output wire order_side,
    output wire [PRICE_W-1:0] order_price,
    output wire [QTY_W-1:0] order_quantity,
    output wire [DEPTH_W-1:0] queried_depth,
    output wire [ID_W-1:0] queried_count,
    output wire bbo_bid_present,
    output wire bbo_ask_present,
    output wire [PRICE_W-1:0] bbo_bid,
    output wire [PRICE_W-1:0] bbo_ask,
    output wire [DEPTH_W-1:0] bbo_bid_qty,
    output wire [DEPTH_W-1:0] bbo_ask_qty,
    output wire [ID_W-1:0] bbo_bid_count,
    output wire [ID_W-1:0] bbo_ask_count,
    output logic [ID_W-1:0] live_count
);
    localparam integer SLOT_ADDR_W = (SLOTS > 1) ? $clog2(SLOTS) : 1;
    localparam integer LEVEL_ADDR_W = (LEVELS > 1) ? $clog2(LEVELS) : 1;

    logic [SLOTS-1:0] live;
    logic entry_side [0:SLOTS-1];
    logic [PRICE_W-1:0] entry_price [0:SLOTS-1];
    logic [QTY_W-1:0] entry_qty [0:SLOTS-1];
    logic [LEVELS-1:0] occupied [0:1];
    logic [DEPTH_W-1:0] depth [0:1][0:LEVELS-1];
    logic [ID_W-1:0] count [0:1][0:LEVELS-1];

    wire id_in_range = id < ID_W'(SLOTS);
    wire replaced_in_range = replacement < ID_W'(SLOTS);
    wire price_in_range = price < PRICE_W'(LEVELS);
    wire [SLOT_ADDR_W-1:0] source_slot = SLOT_ADDR_W'(id);
    wire [SLOT_ADDR_W-1:0] target_slot = SLOT_ADDR_W'(replacement);
    wire [LEVEL_ADDR_W-1:0] target_level = LEVEL_ADDR_W'(price);
    wire source_live = id_in_range && live[source_slot];
    wire target_live = replaced_in_range && live[target_slot];
    wire stored_side = entry_side[source_slot];
    wire [PRICE_W-1:0] stored_price = entry_price[source_slot];
    wire [QTY_W-1:0] stored_qty = entry_qty[source_slot];
    wire [LEVEL_ADDR_W-1:0] source_level = LEVEL_ADDR_W'(stored_price);

    wire adds = op == `MOP_ADD || op == `MOP_SNAP_ADD;
    wire replaces = op == `MOP_REPLACE;
    wire removes = op == `MOP_DELETE || (op == `MOP_EXECUTE && quantity == stored_qty);
    wire [QTY_W-1:0] removed_qty =
        (op == `MOP_DELETE || replaces) ? stored_qty : quantity;
    wire requested_side = replaces ? stored_side : side;
    wire requested_occupied = occupied[requested_side][target_level];
    wire source_last = count[stored_side][source_level] == ID_W'(1);
    wire same_level = replaces && stored_price == price;

    assign order_present = source_live;
    assign order_side = source_live ? stored_side : 1'b0;
    assign order_price = source_live ? stored_price : '0;
    assign order_quantity = source_live ? stored_qty : '0;
    assign queried_depth = price_in_range && occupied[side][target_level] ? depth[side][target_level] : '0;
    assign queried_count = price_in_range && occupied[side][target_level] ? count[side][target_level] : '0;

    market_best_price #(.LEVELS(LEVELS), .HIGH(1'b1)) bid_selector (
        .occupied(occupied[0]), .valid(bbo_bid_present), .price(bbo_bid)
    );
    market_best_price #(.LEVELS(LEVELS), .HIGH(1'b0)) ask_selector (
        .occupied(occupied[1]), .valid(bbo_ask_present), .price(bbo_ask)
    );
    assign bbo_bid_qty = bbo_bid_present ? depth[0][LEVEL_ADDR_W'(bbo_bid)] : '0;
    assign bbo_ask_qty = bbo_ask_present ? depth[1][LEVEL_ADDR_W'(bbo_ask)] : '0;
    assign bbo_bid_count = bbo_bid_present ? count[0][LEVEL_ADDR_W'(bbo_bid)] : '0;
    assign bbo_ask_count = bbo_ask_present ? count[1][LEVEL_ADDR_W'(bbo_ask)] : '0;

    always_comb begin
        error_code = `ME_OK;
        if (!id_in_range)
            error_code = `ME_ID;
        else if (adds) begin
            if (!price_in_range)
                error_code = `ME_PRICE;
            else if (quantity == 0)
                error_code = `ME_QTY;
            else if (source_live)
                error_code = `ME_DUPLICATE;
        end else if (!source_live) begin
            error_code = `ME_ABSENT;
        end else if (replaces) begin
            if (!replaced_in_range)
                error_code = `ME_ID;
            else if (replacement == id)
                error_code = `ME_REPLACE_ID;
            else if (target_live)
                error_code = `ME_DUPLICATE;
            else if (!price_in_range)
                error_code = `ME_PRICE;
            else if (quantity == 0)
                error_code = `ME_QTY;
        end else if (op == `MOP_CANCEL) begin
            if (quantity == 0 || quantity >= stored_qty)
                error_code = `ME_QTY;
        end else if (op == `MOP_EXECUTE) begin
            if (quantity == 0 || quantity > stored_qty)
                error_code = `ME_QTY;
        end
    end

    always_ff @(posedge clk) begin
        if (rst || clear_book) begin
            live <= '0;
            live_count <= '0;
            occupied[0] <= '0;
            occupied[1] <= '0;
        end else if (commit) begin
            if (adds) begin
                live[source_slot] <= 1'b1;
                live_count <= live_count + ID_W'(1);
                entry_side[source_slot] <= side;
                entry_price[source_slot] <= price;
                entry_qty[source_slot] <= quantity;
            end else if (replaces) begin
                live[source_slot] <= 1'b0;
                live[target_slot] <= 1'b1;
                entry_side[target_slot] <= stored_side;
                entry_price[target_slot] <= price;
                entry_qty[target_slot] <= quantity;
            end else if (removes) begin
                live[source_slot] <= 1'b0;
                live_count <= live_count - ID_W'(1);
            end else begin
                entry_qty[source_slot] <= stored_qty - quantity;
            end

            if (same_level) begin
                depth[stored_side][source_level] <=
                    depth[stored_side][source_level] - DEPTH_W'(stored_qty) + DEPTH_W'(quantity);
            end else begin
                if (!adds) begin
                    depth[stored_side][source_level] <= depth[stored_side][source_level] - DEPTH_W'(removed_qty);
                    if (replaces || removes) begin
                        count[stored_side][source_level] <= count[stored_side][source_level] - ID_W'(1);
                        if (source_last)
                            occupied[stored_side][source_level] <= 1'b0;
                    end
                end

                if (adds || replaces) begin
                    occupied[requested_side][target_level] <= 1'b1;
                    count[requested_side][target_level] <=
                        requested_occupied ? count[requested_side][target_level] + ID_W'(1) : ID_W'(1);
                    depth[requested_side][target_level] <= requested_occupied
                        ? depth[requested_side][target_level] + DEPTH_W'(quantity) : DEPTH_W'(quantity);
                end
            end
        end
    end
endmodule

`default_nettype wire
