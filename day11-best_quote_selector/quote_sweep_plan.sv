`timescale 1ns/1ps
`default_nettype none
`include "quote_sweep_codes.svh"

module quote_sweep_plan #(
    parameter integer INSTRUMENTS = 4,
    parameter integer VENUES = 4,
    parameter integer PRICE_W = 20,
    parameter integer QTY_W = 14,
    parameter integer TIME_W = 16,
    parameter integer SEQ_W = 12,
    parameter integer TAG_W = 16,
    parameter integer FEE_W = 12,
    parameter integer EPOCH_W = 16,
    localparam integer INST_W = $clog2(INSTRUMENTS+1),
    localparam integer VENUE_W = $clog2(VENUES+1),
    localparam integer REQUEST_W = QTY_W + $clog2(VENUES),
    localparam integer UNIT_W = (PRICE_W+1 > FEE_W ? PRICE_W+1 : FEE_W) + 1,
    localparam integer VALUE_W = UNIT_W + REQUEST_W,
    localparam integer RECORD_W = 2*(PRICE_W+QTY_W) + TIME_W + SEQ_W,
    localparam integer CHILD_W = VENUE_W + PRICE_W + QTY_W + FEE_W + TIME_W + SEQ_W
) (
    input wire clk, rst,
    input wire [TIME_W-1:0] now_tick,

    input wire query_valid,
    output wire query_ready,
    input wire [INST_W-1:0] query_instrument,
    input wire [TAG_W-1:0] query_tag,
    input wire query_buy,
    input wire [REQUEST_W-1:0] query_quantity,
    input wire [PRICE_W-1:0] query_limit,
    input wire [TIME_W-1:0] query_max_age,
    input wire [VENUES-1:0] query_venues,
    input wire query_all_or_none,

    input wire [VENUES-1:0] read_enabled, read_present,
    input wire [VENUES*RECORD_W-1:0] read_records,
    input wire [VENUES-1:0] policy_enabled,
    input wire [VENUES*QTY_W-1:0] policy_caps,
    input wire [VENUES*FEE_W-1:0] policy_fees,
    input wire [EPOCH_W-1:0] policy_epoch,

    output logic result_valid,
    input wire result_ready,
    output logic [3:0] result_status,
    output logic [TAG_W-1:0] result_tag,
    output logic [TIME_W-1:0] result_tick,
    output logic [EPOCH_W-1:0] result_epoch,
    output logic [VENUES-1:0] result_eligible, result_stale,
    output logic [VENUE_W-1:0] result_count,
    output logic [REQUEST_W-1:0] result_quantity,
    output logic signed [VALUE_W-1:0] result_value,
    output logic [VENUES*CHILD_W-1:0] result_children
);
    localparam integer INDEX_W = VENUES > 1 ? $clog2(VENUES) : 1;
    logic busy;
    logic buy, all_or_none;
    logic [REQUEST_W-1:0] requested, remaining;
    logic signed [VALUE_W-1:0] accumulated_cost;
    logic [VENUES-1:0] unused_candidates;
    logic signed [UNIT_W-1:0] normalized_price [0:VENUES-1];
    logic [PRICE_W-1:0] price [0:VENUES-1];
    logic [QTY_W-1:0] capacity [0:VENUES-1];
    logic [FEE_W-1:0] fee [0:VENUES-1];
    logic [TIME_W-1:0] age [0:VENUES-1];
    logic [SEQ_W-1:0] quote_sequence [0:VENUES-1];

    logic [VENUES-1:0] snapshot_eligible, snapshot_stale;
    logic winner_found;
    logic [INDEX_W-1:0] winner;
    logic signed [UNIT_W-1:0] winner_cost;
    logic [3:0] query_status;
    wire result_space = !result_valid || result_ready;

    assign query_ready = !rst && !busy && result_space;
    wire snapshot = query_valid && query_ready;

    always @* begin
        query_status = `SWEEP_FILLED;
        if (int'(query_instrument) >= INSTRUMENTS)
            query_status = `SWEEP_BAD_INST;
        else if (query_max_age[TIME_W-1])
            query_status = `SWEEP_BAD_AGE;
        else if (query_quantity == '0)
            query_status = `SWEEP_BAD_QTY;
        else if (query_limit == '0)
            query_status = `SWEEP_BAD_LIMIT;
    end

    for (genvar v = 0; v < VENUES; v++) begin : g_snapshot
        wire [SEQ_W-1:0] serial_number;
        wire [TIME_W-1:0] update_tick;
        wire [QTY_W-1:0] bid_qty, ask_qty;
        wire [PRICE_W-1:0] bid_price, ask_price;
        assign {serial_number, update_tick, bid_qty, ask_qty, bid_price, ask_price} =
            read_records[v*RECORD_W +: RECORD_W];

        wire [PRICE_W-1:0] selected_price = query_buy ? ask_price : bid_price;
        wire [QTY_W-1:0] selected_qty = query_buy ? ask_qty : bid_qty;
        wire [QTY_W-1:0] cap = policy_caps[v*QTY_W +: QTY_W];
        wire signed [FEE_W-1:0] selected_fee = policy_fees[v*FEE_W +: FEE_W];
        wire [TIME_W-1:0] elapsed = now_tick - update_tick;
        wire have_side = read_enabled[v] && read_present[v] && selected_qty != '0;
        wire at_limit = query_buy ? selected_price <= query_limit : selected_price >= query_limit;
        wire signed [UNIT_W-1:0] signed_price = $signed(UNIT_W'(selected_price));
        wire signed [UNIT_W-1:0] signed_fee = UNIT_W'(selected_fee);

        assign snapshot_stale[v] = query_status == `SWEEP_FILLED && have_side &&
                                    (elapsed[TIME_W-1] || elapsed > query_max_age);
        assign snapshot_eligible[v] = query_status == `SWEEP_FILLED && have_side &&
            !snapshot_stale[v] && policy_enabled[v] && query_venues[v] && cap != '0 && at_limit;

        always_ff @(posedge clk) begin
            if (snapshot) begin
                // Use one ascending-cost comparison for both sides. A sell
                // ranks by fee less bid so larger net proceeds win.
                normalized_price[v] <= signed_fee + (query_buy ? signed_price : -signed_price);
                price[v] <= selected_price;
                capacity[v] <= selected_qty < cap ? selected_qty : cap;
                fee[v] <= selected_fee;
                age[v] <= elapsed;
                quote_sequence[v] <= serial_number;
            end
        end
    end

    always @* begin
        winner_found = 1'b0;
        winner = '0;
        winner_cost = '0;
        for (integer row = 0; row < VENUES; row++) begin
            if (unused_candidates[row] && (!winner_found || normalized_price[row] < winner_cost)) begin
                winner_found = 1'b1;
                winner = INDEX_W'(row);
                winner_cost = normalized_price[row];
            end
        end
    end

    wire [QTY_W-1:0] child_qty =
        remaining < REQUEST_W'(capacity[winner]) ? QTY_W'(remaining) : capacity[winner];
    wire [REQUEST_W-1:0] remaining_after = remaining - REQUEST_W'(child_qty);
    wire signed [QTY_W:0] signed_child_qty = $signed({1'b0,child_qty});
    wire signed [VALUE_W-1:0] child_cost = winner_cost * signed_child_qty;
    wire signed [VALUE_W-1:0] cost_after = accumulated_cost + child_cost;
    wire [VENUES-1:0] candidates_after = unused_candidates & ~(VENUES'(1) << winner);
    wire done_after = remaining_after == '0 || candidates_after == '0;
    wire [CHILD_W-1:0] child = {
        VENUE_W'(winner), price[winner], child_qty, fee[winner], quote_sequence[winner], age[winner]
    };

    always_ff @(posedge clk) begin
        if (rst) begin
            busy <= 1'b0;
            result_valid <= 1'b0;
        end else begin
            if (result_ready)
                result_valid <= 1'b0;

            if (snapshot) begin
                buy <= query_buy;
                all_or_none <= query_all_or_none;
                requested <= query_quantity;
                remaining <= query_quantity;
                accumulated_cost <= '0;
                unused_candidates <= snapshot_eligible;
                busy <= |snapshot_eligible;
                result_valid <= !(|snapshot_eligible);
                if (query_status != `SWEEP_FILLED)
                    result_status <= query_status;
                else
                    result_status <= query_all_or_none ? `SWEEP_AON_UNFILLED : `SWEEP_NO_LIQUIDITY;
                result_tag <= query_tag;
                result_tick <= now_tick;
                result_epoch <= policy_epoch;
                result_eligible <= snapshot_eligible;
                result_stale <= snapshot_stale;
                result_count <= '0;
                result_quantity <= '0;
                result_value <= '0;
                result_children <= '0;
            end else if (busy) begin
                if (done_after && all_or_none && remaining_after != '0) begin
                    result_status <= `SWEEP_AON_UNFILLED;
                    result_count <= '0;
                    result_children <= '0;
                end else begin
                    result_children[CHILD_W*result_count +: CHILD_W] <= child;
                    result_count <= result_count + 1'b1;
                    if (done_after) begin
                        result_status <= remaining_after == '0 ? `SWEEP_FILLED : `SWEEP_PARTIAL;
                        result_quantity <= requested - remaining_after;
                        result_value <= buy ? cost_after : -cost_after;
                    end
                end
                unused_candidates <= candidates_after;
                remaining <= remaining_after;
                accumulated_cost <= cost_after;
                busy <= !done_after;
                result_valid <= done_after;
            end
        end
    end
endmodule

`default_nettype wire
