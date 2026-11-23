`timescale 1ns/1ps
`default_nettype none
`include "matching_codes.svh"

module matching_book #(
    parameter integer PRICE_LEVELS = 12,
    parameter integer ORDER_SLOTS = 12,
    parameter integer QTY_W = 16,
    localparam integer PRICE_W = $clog2(PRICE_LEVELS + 1),
    localparam integer ID_W = $clog2(ORDER_SLOTS + 1),
    localparam integer LEVEL_QTY_W = QTY_W + ID_W
) (
    input wire                   clk,
    input wire                   rst,
    input wire                   s_valid,
    output wire                  s_ready,
    input wire [1:0]             s_opcode,
    input wire [1:0]             s_tif,
    input wire                   s_is_ask,
    input wire [ID_W-1:0]        s_order_id,
    input wire [PRICE_W-1:0]     s_price,
    input wire [QTY_W-1:0]       s_qty,
    output wire                  m_valid,
    input wire                   m_ready,
    output logic [2:0]           m_kind,
    output logic                 m_last,
    output wire [ID_W-1:0]       m_order_id,
    output logic                 m_is_ask,
    output logic [ID_W-1:0]      m_maker_id,
    output logic [PRICE_W-1:0]   m_price,
    output logic [QTY_W-1:0]     m_qty,
    output logic [QTY_W-1:0]     m_remaining,
    output logic [3:0]           m_reject,
    output logic [LEVEL_QTY_W-1:0] m_level_qty,
    output logic [ID_W-1:0]      m_level_count
);
    localparam logic [ID_W-1:0] NO_ORDER = ID_W'(ORDER_SLOTS);
    localparam integer SLOT_ADDR_W = (ORDER_SLOTS > 1) ? $clog2(ORDER_SLOTS) : 1;
    localparam integer LEVEL_ADDR_W = (PRICE_LEVELS > 1) ? $clog2(PRICE_LEVELS) : 1;

    logic [ORDER_SLOTS-1:0] live;
    logic [PRICE_LEVELS-1:0] bid_occupied, ask_occupied;
    logic [ID_W-1:0] head_id [0:1][0:PRICE_LEVELS-1];
    logic [ID_W-1:0] tail_id [0:1][0:PRICE_LEVELS-1];
    logic [LEVEL_QTY_W-1:0] level_qty [0:1][0:PRICE_LEVELS-1];
    logic [ID_W-1:0] level_count [0:1][0:PRICE_LEVELS-1];
    logic [ID_W-1:0] previous_id [0:ORDER_SLOTS-1];
    logic [ID_W-1:0] next_id [0:ORDER_SLOTS-1];
    logic [PRICE_W-1:0] order_price [0:ORDER_SLOTS-1];
    logic [QTY_W-1:0] order_qty [0:ORDER_SLOTS-1];
    logic order_is_ask [0:ORDER_SLOTS-1];

    logic active;
    logic [1:0] cmd_opcode, cmd_tif;
    logic cmd_is_ask;
    logic [ID_W-1:0] cmd_id;
    logic [PRICE_W-1:0] cmd_price;
    logic [QTY_W-1:0] remaining;

    wire [SLOT_ADDR_W-1:0] cmd_slot = SLOT_ADDR_W'(cmd_id);
    wire [LEVEL_ADDR_W-1:0] cmd_level = LEVEL_ADDR_W'(cmd_price);
    wire cmd_occupied = cmd_is_ask ? ask_occupied[cmd_level] : bid_occupied[cmd_level];
    wire [ID_W-1:0] append_tail = tail_id[cmd_is_ask][cmd_level];
    wire stored_side = order_is_ask[cmd_slot];
    wire [PRICE_W-1:0] stored_price = order_price[cmd_slot];
    wire [QTY_W-1:0] stored_qty = order_qty[cmd_slot];

    wire bid_valid, ask_valid;
    wire [PRICE_W-1:0] bid_price, ask_price;
    wire [ID_W-1:0] bid_head, ask_head;
    wire [PRICE_LEVELS*ID_W-1:0] bid_heads, ask_heads;

    for (genvar p = 0; p < PRICE_LEVELS; p++) begin : gen_heads
        assign bid_heads[p*ID_W+:ID_W] = head_id[0][p];
        assign ask_heads[p*ID_W+:ID_W] = head_id[1][p];
    end

    price_priority #(.LEVELS(PRICE_LEVELS), .HEAD_W(ID_W), .HIGH_FIRST(1'b1)) select_bid (
        .occupied(bid_occupied), .heads(bid_heads), .valid(bid_valid),
        .price(bid_price), .head(bid_head)
    );

    price_priority #(.LEVELS(PRICE_LEVELS), .HEAD_W(ID_W), .HIGH_FIRST(1'b0)) select_ask (
        .occupied(ask_occupied), .heads(ask_heads), .valid(ask_valid),
        .price(ask_price), .head(ask_head)
    );

    wire opposite_valid = cmd_is_ask ? bid_valid : ask_valid;
    wire [PRICE_W-1:0] opposite_price = cmd_is_ask ? bid_price : ask_price;
    wire crosses = opposite_valid &&
                   (cmd_is_ask ? opposite_price >= cmd_price : opposite_price <= cmd_price);
    wire [ID_W-1:0] maker_id = cmd_is_ask ? bid_head : ask_head;
    wire [SLOT_ADDR_W-1:0] maker_slot = SLOT_ADDR_W'(maker_id);
    wire [QTY_W-1:0] maker_qty = order_qty[maker_slot];
    wire [QTY_W:0] taker_diff = {1'b0, remaining} - {1'b0, maker_qty};
    wire [QTY_W-1:0] taker_left = taker_diff[QTY_W-1:0];
    wire [QTY_W-1:0] maker_left = maker_qty - remaining;
    wire maker_depleted = !taker_diff[QTY_W];
    wire taker_continues = maker_depleted && (remaining != maker_qty);

    wire modify_existing = (cmd_opcode != `OP_NEW);
    wire [SLOT_ADDR_W-1:0] changed_slot = modify_existing ? cmd_slot : maker_slot;
    wire changed_side = modify_existing ? stored_side : !cmd_is_ask;
    wire [LEVEL_ADDR_W-1:0] changed_level = modify_existing
        ? LEVEL_ADDR_W'(stored_price) : LEVEL_ADDR_W'(opposite_price);
    wire [ID_W-1:0] removed_previous = modify_existing ? previous_id[cmd_slot] : NO_ORDER;
    wire [ID_W-1:0] removed_next = next_id[changed_slot];
    wire unlink_head = (removed_previous == NO_ORDER);
    wire unlink_tail = (removed_next == NO_ORDER);
    wire unlink_order = (m_kind == `EV_CANCEL) || (m_kind == `EV_FILL && maker_depleted);

    assign s_ready = !rst && !active;
    assign m_valid = !rst && active;
    assign m_order_id = cmd_id;

    always_comb begin
        m_kind = `EV_REJECT;
        m_last = 1'b1;
        m_is_ask = 1'b0;
        m_maker_id = '0;
        m_price = '0;
        m_qty = '0;
        m_remaining = '0;
        m_reject = '0;
        m_level_qty = '0;
        m_level_count = '0;
        if (cmd_opcode == `OP_LEVEL) begin
            if (cmd_price >= PRICE_W'(PRICE_LEVELS)) begin
                m_reject = `ERR_PRICE;
            end else begin
                m_kind = `EV_LEVEL;
                m_is_ask = cmd_is_ask;
                m_price = cmd_price;
                if (cmd_occupied) begin
                    m_level_qty = level_qty[cmd_is_ask][cmd_level];
                    m_level_count = level_count[cmd_is_ask][cmd_level];
                end
            end
        end else if (cmd_id >= NO_ORDER) begin
            m_reject = `ERR_ID;
        end else if (modify_existing) begin
            if (!live[cmd_slot]) begin
                m_reject = `ERR_ABSENT;
            end else if (cmd_opcode == `OP_CANCEL) begin
                m_kind = `EV_CANCEL;
                m_is_ask = stored_side;
                m_price = stored_price;
                m_qty = stored_qty;
            end else if (remaining == '0 || remaining >= stored_qty) begin
                m_reject = `ERR_REDUCE;
            end else begin
                m_kind = `EV_REDUCE;
                m_is_ask = stored_side;
                m_price = stored_price;
                m_qty = stored_qty - remaining;
                m_remaining = remaining;
            end
        end else if (cmd_price >= PRICE_W'(PRICE_LEVELS)) begin
            m_reject = `ERR_PRICE;
        end else if (remaining == '0) begin
            m_reject = `ERR_QTY;
        end else if (cmd_tif > `TIF_POST) begin
            m_reject = `ERR_TIF;
        end else if (live[cmd_slot]) begin
            m_reject = `ERR_DUP;
        end else if (crosses && cmd_tif == `TIF_POST) begin
            m_reject = `ERR_POST;
        end else if (crosses) begin
            m_kind = `EV_FILL;
            m_last = !taker_continues;
            m_is_ask = cmd_is_ask;
            m_maker_id = maker_id;
            m_price = opposite_price;
            m_qty = taker_continues ? maker_qty : remaining;
            m_remaining = taker_continues ? taker_left : '0;
        end else begin
            m_kind = (cmd_tif == `TIF_IOC) ? `EV_EXPIRE : `EV_REST;
            m_is_ask = cmd_is_ask;
            m_price = cmd_price;
            m_qty = remaining;
            m_remaining = (cmd_tif == `TIF_IOC) ? QTY_W'(0) : remaining;
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            active <= 1'b0;
            cmd_opcode <= '0;
            cmd_tif <= '0;
            cmd_is_ask <= 1'b0;
            cmd_id <= '0;
            cmd_price <= '0;
            remaining <= '0;
            live <= '0;
            bid_occupied <= '0;
            ask_occupied <= '0;
        end else if (s_valid && s_ready) begin
            active <= 1'b1;
            cmd_opcode <= s_opcode;
            cmd_tif <= s_tif;
            cmd_is_ask <= s_is_ask;
            cmd_id <= s_order_id;
            cmd_price <= s_price;
            remaining <= s_qty;
        end else if (m_valid && m_ready) begin
            if (m_last)
                active <= 1'b0;
            else
                remaining <= m_remaining;

            if (unlink_order) begin
                live[changed_slot] <= 1'b0;
                if (!unlink_head)
                    next_id[SLOT_ADDR_W'(removed_previous)] <= removed_next;
                else if (!unlink_tail)
                    head_id[changed_side][changed_level] <= removed_next;
                if (!unlink_tail)
                    previous_id[SLOT_ADDR_W'(removed_next)] <= removed_previous;
                else if (!unlink_head)
                    tail_id[changed_side][changed_level] <= removed_previous;
                if (unlink_head && unlink_tail) begin
                    if (changed_side)
                        ask_occupied[changed_level] <= 1'b0;
                    else
                        bid_occupied[changed_level] <= 1'b0;
                end
                level_count[changed_side][changed_level] <=
                    level_count[changed_side][changed_level] - ID_W'(1);
                level_qty[changed_side][changed_level] <=
                    level_qty[changed_side][changed_level] - LEVEL_QTY_W'(m_qty);
            end else if (m_kind == `EV_FILL || m_kind == `EV_REDUCE) begin
                order_qty[changed_slot] <= (m_kind == `EV_FILL) ? maker_left : remaining;
                level_qty[changed_side][changed_level] <=
                    level_qty[changed_side][changed_level] - LEVEL_QTY_W'(m_qty);
            end else if (m_kind == `EV_REST) begin
                live[cmd_slot] <= 1'b1;
                order_price[cmd_slot] <= cmd_price;
                order_qty[cmd_slot] <= remaining;
                order_is_ask[cmd_slot] <= cmd_is_ask;
                next_id[cmd_slot] <= NO_ORDER;
                tail_id[cmd_is_ask][cmd_level] <= cmd_id;
                if (cmd_occupied) begin
                    previous_id[cmd_slot] <= append_tail;
                    next_id[SLOT_ADDR_W'(append_tail)] <= cmd_id;
                    level_count[cmd_is_ask][cmd_level] <=
                        level_count[cmd_is_ask][cmd_level] + ID_W'(1);
                    level_qty[cmd_is_ask][cmd_level] <=
                        level_qty[cmd_is_ask][cmd_level] + LEVEL_QTY_W'(remaining);
                end else begin
                    previous_id[cmd_slot] <= NO_ORDER;
                    head_id[cmd_is_ask][cmd_level] <= cmd_id;
                    level_count[cmd_is_ask][cmd_level] <= ID_W'(1);
                    level_qty[cmd_is_ask][cmd_level] <= LEVEL_QTY_W'(remaining);
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
