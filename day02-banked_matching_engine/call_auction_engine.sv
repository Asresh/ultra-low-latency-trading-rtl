`timescale 1ns/1ps
`default_nettype none
`include "auction_codes.svh"

// A single-instrument, single-price auction. Collection keeps per-tick
// histograms so price discovery scans one tick per cycle independent of depth.
module call_auction_engine #(
    parameter integer PRICES = 16,
    parameter integer SLOTS = 16,
    parameter integer QTY_W = 16,
    parameter integer OWNER_W = 8,
    parameter integer EPOCH_W = 16,
    parameter integer TAG_W = 16,
    localparam integer PRICE_W = $clog2(PRICES+1),
    localparam integer ID_W = $clog2(SLOTS+1),
    localparam integer SUM_W = QTY_W + ID_W
) (
    input  wire clk,
    input  wire rst,
    input  wire s_valid,
    output wire s_ready,
    input  wire [2:0] s_op,
    input  wire [EPOCH_W-1:0] s_epoch,
    input  wire [TAG_W-1:0] s_tag,
    input  wire [ID_W-1:0] s_id,
    input  wire [OWNER_W-1:0] s_owner,
    input  wire s_side,
    input  wire s_market,
    input  wire s_carry,
    input  wire s_reference_half,
    input  wire [PRICE_W-1:0] s_price,
    input  wire [PRICE_W-1:0] s_low,
    input  wire [PRICE_W-1:0] s_high,
    input  wire [QTY_W-1:0] s_quantity,
    output logic m_valid,
    input  wire m_ready,
    output logic [3:0] m_kind,
    output logic [3:0] m_reason,
    output logic m_last,
    output wire [EPOCH_W-1:0] m_epoch,
    output wire [TAG_W-1:0] m_tag,
    output logic [ID_W-1:0] m_first,
    output logic [ID_W-1:0] m_second,
    output logic [OWNER_W-1:0] m_first_owner,
    output logic [OWNER_W-1:0] m_second_owner,
    output logic m_side,
    output logic m_market,
    output logic m_carry,
    output logic m_reference_half,
    output logic [PRICE_W-1:0] m_price,
    output logic [SUM_W-1:0] m_quantity,
    output logic [SUM_W-1:0] m_buys,
    output logic [SUM_W-1:0] m_sells,
    output logic [ID_W-1:0] m_count,
    output logic [ID_W-1:0] m_carried,
    output logic [ID_W-1:0] m_expired
);
    localparam integer SLOT_ADDR_W = SLOTS > 1 ? $clog2(SLOTS) : 1;
    localparam integer TICK_ADDR_W = PRICES > 1 ? $clog2(PRICES) : 1;
    localparam logic [2:0] RECEIVE = 0, REPLY = 1, SCAN = 2, SUMMARY = 3,
                           PAIR = 4, RESIDUE = 5, COMPLETE = 6;
    logic [2:0] state;
    logic collecting;
    logic [EPOCH_W-1:0] epoch, request_epoch;
    logic [TAG_W-1:0] request_tag;
    logic [2:0] op;
    logic [ID_W-1:0] id;
    logic [OWNER_W-1:0] owner_id;
    logic request_side, request_market, request_carry, request_reference_half;
    logic [PRICE_W-1:0] price, low_price, high_price;
    logic [QTY_W-1:0] amount;
    wire [SLOT_ADDR_W-1:0] slot = SLOT_ADDR_W'(id);

    logic [SLOTS-1:0] live;
    logic [ID_W-1:0] live_count;
    logic [ID_W-1:0] age [0:SLOTS-1];
    logic [OWNER_W-1:0] owner [0:SLOTS-1];
    logic side [0:SLOTS-1];
    logic market [0:SLOTS-1];
    logic carry [0:SLOTS-1];
    logic [PRICE_W-1:0] limit [0:SLOTS-1];
    logic [QTY_W-1:0] qty [0:SLOTS-1];

    logic [SUM_W-1:0] bid_bins [0:PRICES-1], ask_bins [0:PRICES-1];
    logic [SUM_W-1:0] bid_total, market_sells;
    logic [PRICE_W-1:0] reference_tick, collar_lo, collar_hi;
    logic reference_half;
    logic [PRICE_W-1:0] tick, best_tick;
    logic [SUM_W-1:0] rolling_buy, rolling_sell, best_buy, best_sell;
    logic [SUM_W-1:0] best_volume, best_gap;
    logic [PRICE_W:0] best_distance;
    logic [SUM_W-1:0] left_to_trade;
    logic [ID_W-1:0] trades, carries, expiries;

    wire [TICK_ADDR_W-1:0] bin_index = TICK_ADDR_W'(
        op == `AU_ADD ? price : limit[slot]
    );
    wire [SUM_W-1:0] buy_now = rolling_buy;
    wire [SUM_W-1:0] sell_now = rolling_sell + ask_bins[TICK_ADDR_W'(tick)];
    wire [SUM_W-1:0] volume = buy_now < sell_now ? buy_now : sell_now;
    wire [SUM_W-1:0] gap = buy_now < sell_now ? sell_now - buy_now : buy_now - sell_now;
    wire [PRICE_W:0] doubled_tick = {tick, 1'b0};
    wire [PRICE_W:0] doubled_reference = {reference_tick, reference_half};
    wire [PRICE_W:0] distance = doubled_tick < doubled_reference ?
        doubled_reference - doubled_tick : doubled_tick - doubled_reference;
    wire better_tick = tick >= collar_lo && (
        tick == collar_lo || volume > best_volume ||
        (volume == best_volume && (gap < best_gap ||
         (gap == best_gap && distance < best_distance)))
    );

    logic have_bid, have_ask, have_residual;
    logic [SLOT_ADDR_W-1:0] first_bid, first_ask, first_residual;
    function automatic logic outranks(
        input logic lhs_market, rhs_market, on_ask,
        input logic [PRICE_W-1:0] lhs_price, rhs_price,
        input logic [ID_W-1:0] lhs_age, rhs_age
    );
        if (lhs_market != rhs_market)
            outranks = lhs_market;
        else if (lhs_market || lhs_price == rhs_price)
            outranks = lhs_age < rhs_age;
        else
            outranks = on_ask ? lhs_price < rhs_price : lhs_price > rhs_price;
    endfunction

    always @* begin : select_order_heads
        have_bid = 0;
        have_ask = 0;
        have_residual = 0;
        first_bid = '0;
        first_ask = '0;
        first_residual = '0;
        for (integer i = 0; i < SLOTS; i++) begin
            if (live[i]) begin
                if (!have_residual || age[i] < age[first_residual]) begin
                    have_residual = 1;
                    first_residual = SLOT_ADDR_W'(i);
                end
                if (!side[i] && (market[i] || limit[i] >= best_tick) &&
                    (!have_bid || outranks(
                        market[i], market[first_bid], 1'b0, limit[i], limit[first_bid], age[i], age[first_bid]
                    ))) begin
                    have_bid = 1;
                    first_bid = SLOT_ADDR_W'(i);
                end
                if (side[i] && (market[i] || limit[i] <= best_tick) &&
                    (!have_ask || outranks(
                        market[i], market[first_ask], 1'b1, limit[i], limit[first_ask], age[i], age[first_ask]
                    ))) begin
                    have_ask = 1;
                    first_ask = SLOT_ADDR_W'(i);
                end
            end
        end
    end

    wire [QTY_W-1:0] pair_size = qty[first_bid] < qty[first_ask] ? qty[first_bid] : qty[first_ask];
    wire common_inactive = id == 0 && owner_id == 0 && !request_side && !request_market && !request_carry;
    wire order_only = !request_side && !request_market && !request_carry && price == 0;
    logic [3:0] decision;
    always @* begin
        decision = `AU_ERR_NONE;
        if ((op == `AU_OPEN) == collecting)
            decision = `AU_ERR_PHASE;
        else if (request_epoch != (op == `AU_OPEN ? epoch+1'b1 : epoch))
            decision = `AU_ERR_EPOCH;
        else if (op == `AU_OPEN) begin
            if (!common_inactive || amount != 0)
                decision = `AU_ERR_FIELDS;
            else if (high_price >= PRICE_W'(PRICES) || low_price > price || price > high_price ||
                     (request_reference_half && price == high_price))
                decision = `AU_ERR_PRICE;
        end else if (low_price != 0 || high_price != 0 || request_reference_half) begin
            decision = `AU_ERR_FIELDS;
        end else if (op == `AU_INDICATE || op == `AU_CLOSE || op == `AU_ABORT) begin
            if (!common_inactive || price != 0 || amount != 0)
                decision = `AU_ERR_FIELDS;
        end else if (id >= ID_W'(SLOTS)) begin
            decision = `AU_ERR_ID;
        end else if (op == `AU_ADD) begin
            if (request_market && (price != 0 || request_carry))
                decision = `AU_ERR_FIELDS;
            else if (price >= PRICE_W'(PRICES))
                decision = `AU_ERR_PRICE;
            else if (amount == 0)
                decision = `AU_ERR_QUANTITY;
            else if (live[slot])
                decision = `AU_ERR_DUPLICATE;
        end else if (!order_only || (op != `AU_REDUCE && amount != 0)) begin
            decision = `AU_ERR_FIELDS;
        end else if (!live[slot]) begin
            decision = `AU_ERR_ABSENT;
        end else if (owner[slot] != owner_id) begin
            decision = `AU_ERR_OWNER;
        end else if (op == `AU_REDUCE && (amount == 0 || amount >= qty[slot])) begin
            decision = `AU_ERR_QUANTITY;
        end
    end

    wire scan_request = state == REPLY && decision == `AU_ERR_NONE &&
        (op == `AU_INDICATE || op == `AU_CLOSE);
    assign s_ready = !rst && state == RECEIVE;
    assign m_tag = request_tag;
    assign m_epoch = request_epoch;

    always @* begin : form_response
        m_valid = 1'b0;
        m_kind = `AU_EV_REJECT;
        m_reason = `AU_ERR_NONE;
        m_last = 1'b0;
        m_first = '0;
        m_second = '0;
        m_first_owner = '0;
        m_second_owner = '0;
        m_side = 1'b0;
        m_market = 1'b0;
        m_carry = 1'b0;
        m_reference_half = 1'b0;
        m_price = '0;
        m_quantity = '0;
        m_buys = '0;
        m_sells = '0;
        m_count = '0;
        m_carried = '0;
        m_expired = '0;
        case (state)
            REPLY: if (!scan_request) begin
                m_valid = !rst;
                m_last = 1'b1;
                m_reason = decision;
                if (decision == `AU_ERR_NONE) begin
                    case (op)
                        `AU_OPEN: begin
                            m_kind = `AU_EV_OPENED;
                            m_price = price;
                            m_reference_half = request_reference_half;
                        end
                        `AU_ABORT: begin
                            m_kind = `AU_EV_ABORTED;
                            m_count = live_count;
                        end
                        default: begin
                            m_first = id;
                            m_first_owner = owner_id;
                            if (op == `AU_ADD) begin
                                m_kind = `AU_EV_ADDED;
                                m_side = request_side;
                                m_market = request_market;
                                m_carry = request_carry;
                                m_price = price;
                                m_quantity = SUM_W'(amount);
                            end else begin
                                m_side = side[slot];
                                m_market = market[slot];
                                m_carry = carry[slot];
                                m_price = limit[slot];
                                m_quantity = SUM_W'(op == `AU_REDUCE ? amount : qty[slot]);
                                case (op)
                                    `AU_CANCEL: m_kind = `AU_EV_CANCELED;
                                    `AU_REDUCE: m_kind = `AU_EV_REDUCED;
                                    default: m_kind = `AU_EV_INSPECTED;
                                endcase
                            end
                        end
                    endcase
                end
            end
            SUMMARY: begin
                m_valid = !rst;
                m_last = op == `AU_INDICATE;
                m_kind = m_last ? `AU_EV_INDICATIVE : `AU_EV_CLEARING;
                m_price = best_tick;
                m_reference_half = reference_half;
                m_quantity = best_volume;
                m_buys = best_buy;
                m_sells = best_sell;
                m_count = live_count;
            end
            PAIR: begin
                m_valid = !rst && have_bid && have_ask;
                m_kind = `AU_EV_TRADE;
                m_price = best_tick;
                m_first = ID_W'(first_bid);
                m_second = ID_W'(first_ask);
                m_first_owner = owner[first_bid];
                m_second_owner = owner[first_ask];
                m_quantity = SUM_W'(pair_size);
            end
            RESIDUE: if (have_residual) begin
                m_valid = !rst;
                m_kind = carry[first_residual] ? `AU_EV_CARRY : `AU_EV_EXPIRE;
                m_first = ID_W'(first_residual);
                m_first_owner = owner[first_residual];
                m_side = side[first_residual];
                m_market = market[first_residual];
                m_carry = carry[first_residual];
                m_price = limit[first_residual];
                m_quantity = SUM_W'(qty[first_residual]);
            end
            COMPLETE: begin
                m_valid = !rst;
                m_last = 1'b1;
                m_kind = `AU_EV_DONE;
                m_price = best_tick;
                m_quantity = best_volume;
                m_count = trades;
                m_carried = carries;
                m_expired = expiries;
            end
            default: ;
        endcase
    end

    wire transfer = m_valid && m_ready;
    wire clear_book = state == REPLY && transfer && decision == `AU_ERR_NONE &&
        (op == `AU_OPEN || op == `AU_ABORT);
    wire add = state == REPLY && transfer && decision == `AU_ERR_NONE && op == `AU_ADD;
    wire cancel = state == REPLY && transfer && decision == `AU_ERR_NONE && op == `AU_CANCEL;
    wire reduce = state == REPLY && transfer && decision == `AU_ERR_NONE && op == `AU_REDUCE;
    wire [SUM_W-1:0] decrement = SUM_W'(qty[slot] - (reduce ? amount : QTY_W'(0)));
    wire removing_bid = qty[first_bid] == pair_size;
    wire removing_ask = qty[first_ask] == pair_size;

    always_ff @(posedge clk) begin : update_orders
        if (rst || clear_book) begin
            live <= '0;
            live_count <= '0;
            bid_total <= '0;
            market_sells <= '0;
            for (integer p = 0; p < PRICES; p++) begin
                bid_bins[p] <= '0;
                ask_bins[p] <= '0;
            end
        end else if (add) begin
            live[slot] <= 1'b1;
            live_count <= live_count + 1'b1;
            age[slot] <= live_count;
            owner[slot] <= owner_id;
            side[slot] <= request_side;
            market[slot] <= request_market;
            carry[slot] <= request_carry;
            limit[slot] <= price;
            qty[slot] <= amount;
            if (!request_side)
                bid_total <= bid_total + SUM_W'(amount);
            if (request_market && request_side)
                market_sells <= market_sells + SUM_W'(amount);
            if (!request_market) begin
                if (request_side)
                    ask_bins[bin_index] <= ask_bins[bin_index] + SUM_W'(amount);
                else
                    bid_bins[bin_index] <= bid_bins[bin_index] + SUM_W'(amount);
            end
        end else if (cancel || reduce) begin
            if (cancel) begin
                live[slot] <= 1'b0;
                live_count <= live_count - 1'b1;
                for (integer i = 0; i < SLOTS; i++)
                    if (live[i] && age[i] > age[slot])
                        age[i] <= age[i] - 1'b1;
            end else begin
                qty[slot] <= amount;
            end
            if (!side[slot])
                bid_total <= bid_total - decrement;
            if (market[slot] && side[slot])
                market_sells <= market_sells - decrement;
            if (!market[slot]) begin
                if (side[slot])
                    ask_bins[bin_index] <= ask_bins[bin_index] - decrement;
                else
                    bid_bins[bin_index] <= bid_bins[bin_index] - decrement;
            end
        end else if (state == PAIR && transfer) begin
            qty[first_bid] <= qty[first_bid] - pair_size;
            qty[first_ask] <= qty[first_ask] - pair_size;
            if (removing_bid)
                live[first_bid] <= 1'b0;
            if (removing_ask)
                live[first_ask] <= 1'b0;
            live_count <= live_count - ID_W'(removing_bid) - ID_W'(removing_ask);
        end else if (state == RESIDUE && transfer) begin
            live[first_residual] <= 1'b0;
            live_count <= live_count - 1'b1;
        end
    end

    always_ff @(posedge clk) begin : auction_sequence
        if (rst) begin
            state <= RECEIVE;
            epoch <= '0;
            collecting <= 1'b0;
            reference_tick <= '0;
            reference_half <= 1'b0;
            collar_lo <= '0;
            collar_hi <= '0;
            {op, request_epoch, request_tag, id, owner_id, request_side, request_market, request_carry,
             request_reference_half,
             price, low_price, high_price, amount} <= '0;
            tick <= '0;
            best_tick <= '0;
            best_volume <= '0;
            best_gap <= '0;
            best_distance <= '0;
            best_buy <= '0;
            best_sell <= '0;
            rolling_buy <= '0;
            rolling_sell <= '0;
            left_to_trade <= '0;
            trades <= '0;
            carries <= '0;
            expiries <= '0;
        end else begin
            case (state)
                RECEIVE: if (s_valid && s_ready) begin
                    {op, request_epoch, request_tag, id, owner_id, request_side, request_market, request_carry,
                     request_reference_half,
                     price, low_price, high_price, amount} <= {
                        s_op, s_epoch, s_tag, s_id, s_owner, s_side, s_market, s_carry,
                        s_reference_half,
                        s_price, s_low, s_high, s_quantity
                    };
                    state <= REPLY;
                end
                REPLY: if (scan_request) begin
                    tick <= '0;
                    rolling_buy <= bid_total;
                    rolling_sell <= market_sells;
                    state <= SCAN;
                end else if (transfer) begin
                    if (decision == `AU_ERR_NONE) begin
                        if (op == `AU_OPEN) begin
                            epoch <= request_epoch;
                            collecting <= 1'b1;
                            reference_tick <= price;
                            reference_half <= request_reference_half;
                            collar_lo <= low_price;
                            collar_hi <= high_price;
                        end else if (op == `AU_ABORT) begin
                            collecting <= 1'b0;
                        end
                    end
                    state <= RECEIVE;
                end
                SCAN: begin
                    if (better_tick) begin
                        best_tick <= tick;
                        best_volume <= volume;
                        best_gap <= gap;
                        best_distance <= distance;
                        best_buy <= buy_now;
                        best_sell <= sell_now;
                    end
                    if (tick == collar_hi) begin
                        state <= SUMMARY;
                    end else begin
                        tick <= tick + 1'b1;
                        rolling_buy <= rolling_buy - bid_bins[TICK_ADDR_W'(tick)];
                        rolling_sell <= sell_now;
                    end
                end
                SUMMARY: if (transfer) begin
                    if (op == `AU_INDICATE) begin
                        state <= RECEIVE;
                    end else begin
                        collecting <= 1'b0;
                        left_to_trade <= best_volume;
                        trades <= '0;
                        carries <= '0;
                        expiries <= '0;
                        state <= best_volume == 0 ? RESIDUE : PAIR;
                    end
                end
                PAIR: if (transfer) begin
                    left_to_trade <= left_to_trade - SUM_W'(pair_size);
                    trades <= trades + 1'b1;
                    if (left_to_trade == SUM_W'(pair_size))
                        state <= RESIDUE;
                end
                RESIDUE: if (!have_residual) begin
                    state <= COMPLETE;
                end else if (transfer) begin
                    if (carry[first_residual])
                        carries <= carries + 1'b1;
                    else
                        expiries <= expiries + 1'b1;
                end
                COMPLETE: if (transfer)
                    state <= RECEIVE;
                default:
                    state <= RECEIVE;
            endcase
        end
    end

`ifdef FORMAL_AUCTION
`include "auction_properties.svh"
`endif
endmodule

`default_nettype wire
