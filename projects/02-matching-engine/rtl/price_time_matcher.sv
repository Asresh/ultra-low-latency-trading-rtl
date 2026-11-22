`timescale 1ns/1ps
`default_nettype none

module price_time_matcher #(
    parameter integer PRICE_LEVELS = 16,
    parameter integer ORDER_SLOTS = 16,
    parameter integer QTY_W = 16,
    localparam integer PRICE_W = $clog2(PRICE_LEVELS + 1),
    localparam integer ID_W = $clog2(ORDER_SLOTS + 1)
) (
    input  wire               clk,
    input  wire               rst,

    input  wire               s_valid,
    output wire               s_ready,
    input  wire               s_cancel,
    input  wire               s_is_ask,
    input  wire [ID_W-1:0]    s_order_id,
    input  wire [PRICE_W-1:0] s_price,
    input  wire [QTY_W-1:0]   s_qty,

    output wire               m_valid,
    input  wire               m_ready,
    output logic [1:0]        m_kind,
    output logic              m_last,
    output wire [ID_W-1:0]    m_order_id,
    output logic              m_is_ask,
    output logic [ID_W-1:0]   m_maker_id,
    output logic [PRICE_W-1:0] m_price,
    output logic [QTY_W-1:0]  m_qty,
    output logic [QTY_W-1:0]  m_remaining,
    output logic [2:0]        m_reject,

    output wire               bid_valid,
    output wire [PRICE_W-1:0] bid_price,
    output wire               ask_valid,
    output wire [PRICE_W-1:0] ask_price
);
    localparam logic [1:0] FILL = 2'd0;
    localparam logic [1:0] REST = 2'd1;
    localparam logic [1:0] CANCEL = 2'd2;
    localparam logic [1:0] REJECT = 2'd3;

    localparam logic [2:0] BAD_ID = 3'd1;
    localparam logic [2:0] BAD_PRICE = 3'd2;
    localparam logic [2:0] BAD_QTY = 3'd3;
    localparam logic [2:0] DUPLICATE = 3'd4;
    localparam logic [2:0] NOT_FOUND = 3'd5;
    localparam logic [ID_W-1:0] NO_ORDER = ID_W'(ORDER_SLOTS);
    localparam integer SLOT_ADDR_W = (ORDER_SLOTS > 1) ? $clog2(ORDER_SLOTS) : 1;
    localparam integer LEVEL_ADDR_W = (PRICE_LEVELS > 1) ? $clog2(PRICE_LEVELS) : 1;

    logic [ORDER_SLOTS-1:0] live;
    logic [PRICE_LEVELS-1:0] bid_occupied, ask_occupied;
    logic [ID_W-1:0] head_id [0:1][0:PRICE_LEVELS-1];
    logic [ID_W-1:0] tail_id [0:1][0:PRICE_LEVELS-1];
    logic [ID_W-1:0] previous_id [0:ORDER_SLOTS-1];
    logic [ID_W-1:0] next_id [0:ORDER_SLOTS-1];
    logic [PRICE_W-1:0] order_price [0:ORDER_SLOTS-1];
    logic [QTY_W-1:0] order_qty [0:ORDER_SLOTS-1];
    logic order_is_ask [0:ORDER_SLOTS-1];

    logic active;
    logic cmd_cancel, cmd_is_ask;
    logic [ID_W-1:0] cmd_id;
    logic [PRICE_W-1:0] cmd_price;
    logic [QTY_W-1:0] remaining;

    // Keep the full-width IDs and prices for validity and sentinel checks.
    // Table addresses need only enough bits for the implemented entries.
    wire [SLOT_ADDR_W-1:0] cmd_slot = SLOT_ADDR_W'(cmd_id);
    wire [LEVEL_ADDR_W-1:0] cmd_level = LEVEL_ADDR_W'(cmd_price);
    wire [ID_W-1:0] append_tail = tail_id[cmd_is_ask][cmd_level];

    wire opposite_valid = cmd_is_ask ? bid_valid : ask_valid;
    wire [PRICE_W-1:0] opposite_price = cmd_is_ask ? bid_price : ask_price;
    wire crosses = opposite_valid &&
                   (cmd_is_ask ? opposite_price >= cmd_price : opposite_price <= cmd_price);
    wire [ID_W-1:0] bid_head, ask_head;
    wire [ID_W-1:0] maker_id = cmd_is_ask ? bid_head : ask_head;
    wire [SLOT_ADDR_W-1:0] maker_slot = SLOT_ADDR_W'(maker_id);
    wire [QTY_W-1:0] maker_qty = order_qty[maker_slot];
    // Compute both residuals directly from the operands. The taker
    // subtraction's borrow selects the outcome and avoids putting the fill
    // quantity mux in front of a second quantity calculation.
    wire [QTY_W:0] taker_diff = {1'b0, remaining} - {1'b0, maker_qty};
    wire [QTY_W-1:0] taker_diff_qty = taker_diff[QTY_W-1:0];
    wire [QTY_W-1:0] maker_diff = maker_qty - remaining;
    wire maker_depleted = !taker_diff[QTY_W];
    wire taker_continues = maker_depleted && (remaining != maker_qty);

    wire [SLOT_ADDR_W-1:0] removed_slot = cmd_cancel ? cmd_slot : maker_slot;
    wire [ID_W-1:0] removed_previous = cmd_cancel ? previous_id[cmd_slot] : NO_ORDER;
    wire [ID_W-1:0] removed_next = next_id[removed_slot];
    wire [LEVEL_ADDR_W-1:0] removed_level = cmd_cancel
        ? LEVEL_ADDR_W'(order_price[cmd_slot]) : LEVEL_ADDR_W'(opposite_price);
    wire removed_is_ask = cmd_cancel ? order_is_ask[cmd_slot] : !cmd_is_ask;

    wire [PRICE_LEVELS*ID_W-1:0] bid_heads, ask_heads;
    for (genvar p = 0; p < PRICE_LEVELS; p++) begin : gen_level_heads
        assign bid_heads[p*ID_W+:ID_W] = head_id[0][p];
        assign ask_heads[p*ID_W+:ID_W] = head_id[1][p];
    end

    price_priority #(.LEVELS(PRICE_LEVELS), .HEAD_W(ID_W), .HIGH_FIRST(1'b1)) select_bid (
        .occupied(bid_occupied), .heads(bid_heads),
        .valid(bid_valid), .price(bid_price), .head(bid_head)
    );
    price_priority #(.LEVELS(PRICE_LEVELS), .HEAD_W(ID_W), .HIGH_FIRST(1'b0)) select_ask (
        .occupied(ask_occupied), .heads(ask_heads),
        .valid(ask_valid), .price(ask_price), .head(ask_head)
    );

    assign s_ready = !rst && !active;
    assign m_valid = !rst && active;
    assign m_order_id = cmd_id;

    // Choose the next event from the current state. The book and command both
    // hold while the sink stalls, so all event fields are stable under stall.
    always_comb begin
        m_kind = REJECT;
        m_last = 1'b1;
        m_is_ask = 1'b0;
        m_maker_id = '0;
        m_price = '0;
        m_qty = '0;
        m_remaining = '0;
        m_reject = '0;
        if (cmd_id >= NO_ORDER) begin
            m_reject = BAD_ID;
        end else if (cmd_cancel) begin
            if (live[cmd_slot]) begin
                m_kind = CANCEL;
                m_is_ask = order_is_ask[cmd_slot];
                m_price = order_price[cmd_slot];
                m_qty = order_qty[cmd_slot];
            end else begin
                m_reject = NOT_FOUND;
            end
        end else if (cmd_price >= PRICE_W'(PRICE_LEVELS)) begin
            m_reject = BAD_PRICE;
        end else if (remaining == '0) begin
            m_reject = BAD_QTY;
        end else if (live[cmd_slot]) begin
            m_reject = DUPLICATE;
        end else if (crosses) begin
            m_kind = FILL;
            m_last = !taker_continues;
            m_is_ask = cmd_is_ask;
            m_maker_id = maker_id;
            m_price = opposite_price;
            m_qty = taker_continues ? maker_qty : remaining;
            m_remaining = taker_continues ? taker_diff_qty : '0;
        end else begin
            m_kind = REST;
            m_is_ask = cmd_is_ask;
            m_price = cmd_price;
            m_qty = remaining;
            m_remaining = remaining;
        end
    end

    // Only validity and occupancy need clearing. An insertion defines every
    // field of a live slot, and initializes the pointers of an empty level.
    always_ff @(posedge clk) begin
        if (rst) begin
            active <= 1'b0;
            cmd_cancel <= 1'b0;
            cmd_is_ask <= 1'b0;
            cmd_id <= '0;
            cmd_price <= '0;
            remaining <= '0;
            live <= '0;
            bid_occupied <= '0;
            ask_occupied <= '0;
        end else if (s_valid && s_ready) begin
            active <= 1'b1;
            cmd_cancel <= s_cancel;
            cmd_is_ask <= s_is_ask;
            cmd_id <= s_order_id;
            cmd_price <= s_price;
            remaining <= s_qty;
        end else if (m_valid && m_ready) begin
            if (m_last) begin
                active <= 1'b0;
            end else begin
                remaining <= m_remaining;
            end

            if (m_kind == CANCEL || (m_kind == FILL && maker_depleted)) begin
                // Fills remove only the head; cancellation can splice either
                // neighbor. Leave head and tail stale when a level empties.
                live[removed_slot] <= 1'b0;
                if (cmd_cancel && removed_previous != NO_ORDER)
                    next_id[SLOT_ADDR_W'(removed_previous)] <= removed_next;
                else if (removed_next != NO_ORDER)
                    head_id[removed_is_ask][removed_level] <= removed_next;
                if (removed_next != NO_ORDER)
                    previous_id[SLOT_ADDR_W'(removed_next)] <= removed_previous;
                else if (cmd_cancel && removed_previous != NO_ORDER)
                    tail_id[removed_is_ask][removed_level] <= removed_previous;
                if (removed_previous == NO_ORDER && removed_next == NO_ORDER) begin
                    if (removed_is_ask)
                        ask_occupied[removed_level] <= 1'b0;
                    else
                        bid_occupied[removed_level] <= 1'b0;
                end
            end else if (m_kind == FILL) begin
                order_qty[maker_slot] <= maker_diff;
            end else if (m_kind == REST) begin
                live[cmd_slot] <= 1'b1;
                order_price[cmd_slot] <= cmd_price;
                order_qty[cmd_slot] <= remaining;
                order_is_ask[cmd_slot] <= cmd_is_ask;
                next_id[cmd_slot] <= NO_ORDER;
                tail_id[cmd_is_ask][cmd_level] <= cmd_id;

                if (cmd_is_ask ? ask_occupied[cmd_level] : bid_occupied[cmd_level]) begin
                    previous_id[cmd_slot] <= append_tail;
                    next_id[SLOT_ADDR_W'(append_tail)] <= cmd_id;
                end else begin
                    previous_id[cmd_slot] <= NO_ORDER;
                    head_id[cmd_is_ask][cmd_level] <= cmd_id;
                    if (cmd_is_ask)
                        ask_occupied[cmd_level] <= 1'b1;
                    else
                        bid_occupied[cmd_level] <= 1'b1;
                end
            end
        end
    end
endmodule

`default_nettype wire
