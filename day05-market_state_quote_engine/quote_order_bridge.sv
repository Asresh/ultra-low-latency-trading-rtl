`timescale 1ns/1ps
`default_nettype none

// Translate quote intentions into acknowledged order actions. The downstream
// adapter discards an ADD unless applicable is high on the transfer edge.
module quote_order_bridge #(
    parameter integer INSTRUMENTS = 3,
    parameter integer PRICE_W = 16,
    parameter integer QTY_W = 16,
    parameter integer EPOCH_W = 8,
    parameter integer GEN_W = 16,
    parameter integer REQUEST_W = 32,
    localparam integer INST_W = $clog2(INSTRUMENTS+1),
    localparam integer SLOTS = INSTRUMENTS * 2,
    localparam integer SLOT_W = $clog2(SLOTS)
) (
    input wire clk,
    input wire rst,

    input wire s_valid,
    output wire s_ready,
    input wire [INST_W-1:0] s_instrument,
    input wire s_active,
    input wire s_current,
    input wire [EPOCH_W-1:0] s_epoch,
    input wire [PRICE_W-1:0] s_bid,
    input wire [PRICE_W-1:0] s_ask,
    input wire [QTY_W-1:0] s_size,
    input wire [INSTRUMENTS-1:0] eligible,
    input wire [INSTRUMENTS*EPOCH_W-1:0] epoch,

    output logic m_valid,
    input wire m_ready,
    output wire m_applicable,
    output logic m_cancel,
    output logic [INST_W-1:0] m_instrument,
    output logic m_ask,
    output logic [GEN_W-1:0] m_generation,
    output logic [REQUEST_W-1:0] m_request,
    output logic [EPOCH_W-1:0] m_epoch,
    output logic [PRICE_W-1:0] m_price,
    output logic [QTY_W-1:0] m_size,

    input wire ack_valid,
    output wire ack_ready,
    input wire ack_cancel,
    input wire [INST_W-1:0] ack_instrument,
    input wire ack_ask,
    input wire [GEN_W-1:0] ack_generation,
    input wire [REQUEST_W-1:0] ack_request,
    input wire ack_ok,
    input wire rearm_valid,
    input wire [INST_W-1:0] rearm_instrument,

    output wire [SLOTS-1:0] rejected,
    output wire [SLOTS-1:0] exhausted,
    output logic [31:0] stale_intents,
    output logic [31:0] bad_intents,
    output logic [31:0] aborted_adds,
    output logic [31:0] stale_acks
);
    localparam logic [REQUEST_W-1:0] LAST_ADD_REQUEST = {REQUEST_W{1'b1}} - 1'b1;
    localparam logic [1:0] EMPTY = 0, ADD_WAIT = 1, LIVE = 2, CANCEL_WAIT = 3;
    logic [INSTRUMENTS-1:0] desired_active;
    logic [EPOCH_W-1:0] desired_epoch [0:INSTRUMENTS-1];
    logic [QTY_W-1:0] desired_size [0:INSTRUMENTS-1];
    logic [PRICE_W-1:0] desired_bid [0:INSTRUMENTS-1];
    logic [PRICE_W-1:0] desired_ask [0:INSTRUMENTS-1];
    logic [1:0] phase [0:SLOTS-1];
    logic [GEN_W-1:0] generation [0:SLOTS-1];
    logic [REQUEST_W-1:0] last_request [0:SLOTS-1];
    logic [EPOCH_W-1:0] order_epoch [0:SLOTS-1];
    logic [PRICE_W-1:0] price [0:SLOTS-1];
    logic [QTY_W-1:0] size [0:SLOTS-1];
    logic [SLOTS-1:0] blocked;
    wire [SLOTS-1:0] want_cancel, want_add, current_key;
    wire [PRICE_W-1:0] desired_price [0:SLOTS-1];
    logic [SLOT_W-1:0] cursor, held_slot;
    logic [SLOT_W-1:0] candidate;
    logic found;
    logic any_cancel;
    wire [SLOT_W-1:0] ack_slot = SLOT_W'({ack_instrument,ack_ask});
    wire [SLOT_W-1:0] opposite = held_slot ^ SLOT_W'(1);
    wire current_ask_safe = phase[opposite] == EMPTY || m_price > price[opposite];
    wire current_bid_safe = phase[opposite] == EMPTY || m_price < price[opposite];
    wire ack_edge = ack_valid && ack_ready;
    wire good_ack = int'(ack_instrument) < INSTRUMENTS &&
                    ack_generation == generation[ack_slot] &&
                    ack_request == last_request[ack_slot] &&
                    phase[ack_slot] == (ack_cancel ? CANCEL_WAIT : ADD_WAIT);
    wire result_edge = m_valid && m_ready && !rst;
    wire valid_instrument = int'(s_instrument) < INSTRUMENTS;
    wire valid_quote = !s_active || (s_size != 0 && s_bid < s_ask);
    wire quote_edge = s_valid && s_ready;
    wire take_quote = quote_edge && valid_instrument && s_current &&
                      s_epoch == epoch[EPOCH_W*int'(s_instrument)+:EPOCH_W];
    wire expires_now = take_quote && s_instrument == m_instrument &&
                       (!s_active || !valid_quote || s_epoch != m_epoch ||
                        (m_ask ? s_ask : s_bid) != m_price || s_size != m_size);

    assign s_ready = !rst;
    assign ack_ready = !rst;
    assign rejected = blocked;
    assign m_applicable = m_valid && !rst && (m_cancel || (
        current_key[held_slot] && !expires_now && m_epoch == desired_epoch[m_instrument] &&
        m_price == desired_price[held_slot] && m_size == desired_size[m_instrument] &&
        (m_ask ? current_ask_safe : current_bid_safe)
    ));

    function automatic logic [31:0] increment(input logic [31:0] count);
        increment = (&count) ? count : count + 1'b1;
    endfunction

    for (genvar bank = 0; bank < INSTRUMENTS; bank = bank+1) begin : g_target
        wire [EPOCH_W-1:0] now_epoch = epoch[bank*EPOCH_W+:EPOCH_W];
        always_ff @(posedge clk) begin
            if (rst) begin
                desired_active[bank] <= 1'b0;
                desired_epoch[bank] <= '0;
                desired_size[bank] <= '0;
                desired_bid[bank] <= '0;
                desired_ask[bank] <= '0;
            end else begin
                if (!eligible[bank] || now_epoch != desired_epoch[bank])
                    desired_active[bank] <= 1'b0;
                if (take_quote && s_instrument == INST_W'(bank)) begin
                    desired_active[bank] <= s_active && valid_quote && eligible[bank];
                    desired_epoch[bank] <= s_epoch;
                    desired_size[bank] <= s_size;
                    desired_bid[bank] <= s_bid;
                    desired_ask[bank] <= s_ask;
                end
            end
        end
    end

    for (genvar slot = 0; slot < SLOTS; slot = slot+1) begin : g_side
        localparam integer BANK = slot/2, PEER = slot^1;
        wire [EPOCH_W-1:0] now_epoch = epoch[BANK*EPOCH_W+:EPOCH_W];
        wire same_order = desired_price[slot] == price[slot] &&
                          desired_size[BANK] == size[slot] &&
                          desired_epoch[BANK] == order_epoch[slot];
        wire safe_side = phase[PEER] == EMPTY ||
                         (slot%2 == 0 ? desired_price[slot] < price[PEER]
                                      : desired_price[slot] > price[PEER]);
        wire rearm_side = (take_quote && s_instrument == INST_W'(BANK)) ||
                          (rearm_valid && rearm_instrument == INST_W'(BANK));
        assign desired_price[slot] = slot%2 == 0 ? desired_bid[BANK] : desired_ask[BANK];
        assign current_key[slot] = desired_active[BANK] && eligible[BANK] &&
                                   desired_epoch[BANK] == now_epoch;
        assign exhausted[slot] = (&generation[slot]) || last_request[slot] >= LAST_ADD_REQUEST;
        assign want_cancel[slot] = phase[slot] == LIVE && !blocked[slot] && !(&last_request[slot]) &&
                                   (!current_key[slot] || !same_order);
        assign want_add[slot] = phase[slot] == EMPTY && !blocked[slot] &&
                                !exhausted[slot] &&
                                current_key[slot] && safe_side;

        always_ff @(posedge clk) begin
            if (rst) begin
                phase[slot] <= EMPTY;
                generation[slot] <= '0;
                last_request[slot] <= '0;
                order_epoch[slot] <= '0;
                price[slot] <= '0;
                size[slot] <= '0;
                blocked[slot] <= 1'b0;
            end else begin
                if (rearm_side)
                    blocked[slot] <= 1'b0;
                if (result_edge && held_slot == SLOT_W'(slot) && m_applicable) begin
                    last_request[slot] <= m_request;
                    if (m_cancel)
                        phase[slot] <= CANCEL_WAIT;
                    else begin
                        phase[slot] <= ADD_WAIT;
                        generation[slot] <= m_generation;
                        order_epoch[slot] <= m_epoch;
                        price[slot] <= m_price;
                        size[slot] <= m_size;
                    end
                end
                if (ack_edge && good_ack && ack_slot == SLOT_W'(slot)) begin
                    // A failed ADD leaves no live order. A failed CANCEL may
                    // leave the order live, so retain its crossing constraint.
                    blocked[slot] <= !ack_ok && !rearm_side;
                    phase[slot] <= ack_ok == ack_cancel ? EMPTY : LIVE;
                end
            end
        end
    end

    always @* begin : select_action
        integer index;
        any_cancel = |want_cancel;
        found = 1'b0;
        candidate = '0;
        for (integer offset = 0; offset < SLOTS; offset++) begin
            index = int'(cursor) + offset;
            if (index >= SLOTS)
                index = index - SLOTS;
            if (!found && (any_cancel ? want_cancel[index] : want_add[index])) begin
                found = 1'b1;
                candidate = SLOT_W'(index);
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            m_valid <= 1'b0;
            m_cancel <= 1'b0;
            m_instrument <= '0;
            m_ask <= 1'b0;
            m_generation <= '0;
            m_request <= '0;
            m_epoch <= '0;
            m_price <= '0;
            m_size <= '0;
            cursor <= '0;
            held_slot <= '0;
            stale_intents <= '0;
            bad_intents <= '0;
            aborted_adds <= '0;
            stale_acks <= '0;
        end else begin
            if (quote_edge && !valid_instrument)
                bad_intents <= increment(bad_intents);
            else if (quote_edge && !take_quote)
                stale_intents <= increment(stale_intents);
            else if (take_quote && !valid_quote)
                bad_intents <= increment(bad_intents);
            if (ack_edge && !good_ack)
                stale_acks <= increment(stale_acks);
            if (result_edge) begin
                m_valid <= 1'b0;
                if (!m_applicable)
                    aborted_adds <= increment(aborted_adds);
            end else if (!m_valid && found) begin
                m_valid <= 1'b1;
                m_cancel <= any_cancel;
                m_instrument <= INST_W'(candidate >> 1);
                m_ask <= candidate[0];
                held_slot <= candidate;
                cursor <= candidate == SLOT_W'(SLOTS-1) ? '0 : candidate + 1'b1;
                m_generation <= any_cancel ? generation[candidate] : generation[candidate] + 1'b1;
                m_request <= last_request[candidate] + 1'b1;
                m_epoch <= any_cancel ? order_epoch[candidate] : desired_epoch[int'(candidate)>>1];
                m_price <= any_cancel ? price[candidate] : desired_price[candidate];
                m_size <= any_cancel ? size[candidate] : desired_size[int'(candidate)>>1];
            end
        end
    end
endmodule

`default_nettype wire
