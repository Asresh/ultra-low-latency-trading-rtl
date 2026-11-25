`timescale 1ns/1ps
`default_nettype none

// Compute asymmetric quote bounds from a live market snapshot, a qualified
// signed inventory, and already reserved leaves on both sides.
module quote_inventory_overlay #(
    parameter integer INSTRUMENTS=3,
    parameter integer LEVELS=128,
    parameter integer POS_W=24,
    parameter integer QTY_W=16,
    parameter integer RESERVE_W=24,
    parameter integer EPOCH_W=8,
    parameter integer REV_W=16,
    localparam integer INST_W=$clog2(INSTRUMENTS+1),
    localparam integer PRICE_W=$clog2(LEVELS+1),
    localparam integer POLICY_W=4*POS_W+2*QTY_W+PRICE_W
) (
    input wire clk,
    input wire rst,
    input wire s_valid,
    output wire s_ready,
    input wire [INST_W-1:0] s_instrument,
    input wire s_active, s_current,
    input wire [EPOCH_W-1:0] s_epoch,
    input wire [PRICE_W-1:0] s_bid, s_ask,
    input wire [QTY_W-1:0] s_size,
    input wire [INSTRUMENTS-1:0] market_eligible,
    input wire [INSTRUMENTS*EPOCH_W-1:0] market_epoch,
    input wire [INSTRUMENTS-1:0] armed_banks, changing_banks,
    input wire [INSTRUMENTS*REV_W-1:0] revisions,
    input wire [INSTRUMENTS*POLICY_W-1:0] policy,
    input wire [INSTRUMENTS*RESERVE_W-1:0] bid_reserved, ask_reserved,

    output wire m_valid,
    input wire m_ready,
    output wire m_current,
    output logic [INST_W-1:0] m_instrument,
    output wire [EPOCH_W-1:0] m_epoch,
    output wire [REV_W-1:0] m_revision,
    output wire [PRICE_W-1:0] m_bid, m_ask,
    output wire [QTY_W-1:0] m_bid_size, m_ask_size,
    output wire signed [POS_W-1:0] m_position,
    output wire [2:0] m_reason,
    output wire [INSTRUMENTS-1:0] bid_eligible, ask_eligible,
    output wire [INSTRUMENTS*QTY_W-1:0] live_bid_size, live_ask_size
);
    localparam logic [2:0] TWO_SIDES=0, ONE_SIDE=1, MARKET_OFF=2, FENCED=3, BAD_QUOTE=4, NO_ROOM=5;
    localparam integer SLOT_W=INSTRUMENTS>1 ? $clog2(INSTRUMENTS) : 1;
    localparam integer MATH0=POS_W>RESERVE_W ? POS_W : RESERVE_W;
    localparam integer MATH_W=(MATH0>QTY_W ? MATH0 : QTY_W)+2;
    localparam integer KEY_W=EPOCH_W+REV_W+2*PRICE_W+2*QTY_W+POS_W+3;
    localparam logic [PRICE_W-1:0] MAX_PRICE=PRICE_W'(LEVELS-1);

    logic [INSTRUMENTS-1:0] have_snapshot;
    logic [EPOCH_W-1:0] captured_epoch [0:INSTRUMENTS-1];
    logic [PRICE_W-1:0] captured_bid [0:INSTRUMENTS-1], captured_ask [0:INSTRUMENTS-1];
    logic [QTY_W-1:0] captured_size [0:INSTRUMENTS-1];
    logic [INSTRUMENTS-1:0] active_snapshot, bad_snapshot;
    logic [INSTRUMENTS-1:0] announced;
    logic [KEY_W-1:0] acknowledged [0:INSTRUMENTS-1];
    wire [KEY_W-1:0] desired [0:INSTRUMENTS-1];
    wire [INSTRUMENTS-1:0] dirty, replaced;
    logic pending;
    logic [KEY_W-1:0] offered;
    logic [SLOT_W-1:0] cursor, selected, held_at;
    logic found;

    assign s_ready = !rst;
    assign m_valid = !rst && pending;
    assign {m_epoch,m_revision,m_position,m_bid,m_ask,m_bid_size,m_ask_size,m_reason} = offered;
    assign m_current = m_valid && desired[held_at] == offered &&
                       !replaced[held_at] && !changing_banks[held_at];

    function automatic logic [QTY_W-1:0] clip_size(
        input logic signed [MATH_W-1:0] room,
        input logic [QTY_W-1:0] desired_size,
        input logic [QTY_W-1:0] max_size
    );
        logic [QTY_W-1:0] cap;
        cap = max_size < desired_size ? max_size : desired_size;
        if (room <= 0)
            clip_size = '0;
        else if (room < $signed(MATH_W'(cap)))
            clip_size = QTY_W'(room);
        else
            clip_size = cap;
    endfunction

    for (genvar bank=0; bank<INSTRUMENTS; bank=bank+1) begin : sizing
        wire signed [POS_W-1:0] position, floor_pos, ceiling_pos;
        wire [QTY_W-1:0] max_bid, max_ask;
        wire [POS_W-1:0] trigger;
        wire [PRICE_W-1:0] ticks;
        assign {position,floor_pos,ceiling_pos,max_bid,max_ask,trigger,ticks} =
            policy[POLICY_W*bank+:POLICY_W];
        wire [EPOCH_W-1:0] epoch = market_epoch[EPOCH_W*bank+:EPOCH_W];
        wire [REV_W-1:0] revision = revisions[REV_W*bank+:REV_W];
        wire quote_here = s_valid && s_ready && s_current &&
                          s_instrument == INST_W'(bank) && s_epoch == epoch;
        wire quote_legal = s_bid < s_ask && s_ask <= MAX_PRICE && s_size != 0;
        wire allowed = market_eligible[bank] && active_snapshot[bank] &&
                       captured_epoch[bank] == epoch;
        wire invalid = market_eligible[bank] && bad_snapshot[bank] && captured_epoch[bank] == epoch;
        wire signed [MATH_W-1:0] position_ext = MATH_W'(position);
        wire signed [MATH_W-1:0] positive_trigger = $signed(MATH_W'(trigger));
        wire signed [MATH_W-1:0] buy_room = MATH_W'(ceiling_pos) - position_ext -
                         $signed(MATH_W'(bid_reserved[RESERVE_W*bank+:RESERVE_W]));
        wire signed [MATH_W-1:0] sell_room = position_ext - MATH_W'(floor_pos) -
                         $signed(MATH_W'(ask_reserved[RESERVE_W*bank+:RESERVE_W]));
        wire [QTY_W-1:0] buy_size = allowed && armed_banks[bank] ?
            clip_size(buy_room,captured_size[bank],max_bid) : '0;
        wire [QTY_W-1:0] sell_size = allowed && armed_banks[bank] ?
            clip_size(sell_room,captured_size[bank],max_ask) : '0;
        wire long_skew = trigger != 0 && position_ext >= positive_trigger;
        wire short_skew = trigger != 0 && position_ext <= -positive_trigger;
        wire [PRICE_W:0] higher_ask = {1'b0,captured_ask[bank]} + {1'b0,ticks};
        wire [PRICE_W-1:0] buy_price = !long_skew ? captured_bid[bank] :
            ticks > captured_bid[bank] ? PRICE_W'(0) : captured_bid[bank]-ticks;
        wire [PRICE_W-1:0] sell_price = !short_skew ? captured_ask[bank] :
            higher_ask > {1'b0,MAX_PRICE} ? MAX_PRICE : PRICE_W'(higher_ask);
        wire [2:0] reason = invalid ? BAD_QUOTE : !allowed ? MARKET_OFF :
                            !armed_banks[bank] ? FENCED :
                            (buy_size == 0 && sell_size == 0) ? NO_ROOM :
                            (buy_size == 0 || sell_size == 0) ? ONE_SIDE : TWO_SIDES;
        wire valid_now = !rst && !changing_banks[bank] && !replaced[bank];
        assign bid_eligible[bank] = valid_now && buy_size != 0;
        assign ask_eligible[bank] = valid_now && sell_size != 0;
        assign live_bid_size[QTY_W*bank+:QTY_W] = valid_now ? buy_size : QTY_W'(0);
        assign live_ask_size[QTY_W*bank+:QTY_W] = valid_now ? sell_size : QTY_W'(0);
        assign desired[bank] = {epoch,revision,position,
            buy_size != 0 ? buy_price : PRICE_W'(0),
            sell_size != 0 ? sell_price : PRICE_W'(0),
            buy_size,sell_size,reason};
        assign dirty[bank] = have_snapshot[bank] &&
                             (!announced[bank] || acknowledged[bank] != desired[bank]);
        assign replaced[bank] = quote_here &&
            ({s_active && quote_legal,s_active && !quote_legal,s_epoch,s_bid,s_ask,s_size} !=
             {active_snapshot[bank],bad_snapshot[bank],captured_epoch[bank],
              captured_bid[bank],captured_ask[bank],captured_size[bank]});

`ifdef FORMAL_INVENTORY
        always_ff @(posedge clk) begin
            if (!rst && buy_size != 0) begin
                assert ($signed(MATH_W'(buy_size)) <= buy_room);
                assert (buy_price <= captured_bid[bank]);
                assert (buy_size <= captured_size[bank] && buy_size <= max_bid);
            end
            if (!rst && sell_size != 0) begin
                assert ($signed(MATH_W'(sell_size)) <= sell_room);
                assert (sell_price >= captured_ask[bank]);
                assert (sell_size <= captured_size[bank] && sell_size <= max_ask);
            end
            if (!rst && buy_size != 0 && sell_size != 0)
                assert (buy_price < sell_price);
        end
`endif

        always_ff @(posedge clk) begin
            if (rst) begin
                have_snapshot[bank] <= 0;
                active_snapshot[bank] <= 0;
                bad_snapshot[bank] <= 0;
                captured_epoch[bank] <= '0;
                captured_bid[bank] <= '0;
                captured_ask[bank] <= '0;
                captured_size[bank] <= '0;
                announced[bank] <= 0;
                acknowledged[bank] <= '0;
            end else begin
                if (!market_eligible[bank] || captured_epoch[bank] != epoch) begin
                    active_snapshot[bank] <= 0;
                    bad_snapshot[bank] <= 0;
                end
                if (quote_here) begin
                    have_snapshot[bank] <= 1;
                    active_snapshot[bank] <= s_active && quote_legal && market_eligible[bank];
                    bad_snapshot[bank] <= s_active && !quote_legal && market_eligible[bank];
                    captured_epoch[bank] <= s_epoch;
                    captured_bid[bank] <= s_bid;
                    captured_ask[bank] <= s_ask;
                    captured_size[bank] <= s_size;
                end
                if (m_valid && m_ready && m_current && held_at == SLOT_W'(bank)) begin
                    announced[bank] <= 1;
                    acknowledged[bank] <= offered;
                end
            end
        end
    end

    always @* begin : find_dirty
        integer index;
        found = 0;
        selected = '0;
        for (integer offset=0;offset<INSTRUMENTS;offset=offset+1) begin
            index = int'(cursor)+offset;
            if (index >= INSTRUMENTS)
                index = index-INSTRUMENTS;
            if (!found && dirty[index]) begin
                found = 1;
                selected = SLOT_W'(index);
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            pending <= 0;
            m_instrument <= '0;
            offered <= '0;
            held_at <= '0;
            cursor <= '0;
        end else if (m_valid && m_ready) begin
            pending <= 0;
        end else if (!pending && found) begin
            pending <= 1;
            held_at <= selected;
            offered <= desired[selected];
            m_instrument <= INST_W'(selected);
            cursor <= selected == SLOT_W'(INSTRUMENTS-1) ? '0 : selected+1'b1;
        end
    end
`ifdef FORMAL_INVENTORY
    logic sampled = 0;
    always_ff @(posedge clk) begin
        sampled <= 1;
        if (!sampled)
            assume (rst);
        if (sampled && !rst && !$past(rst)) begin
            if ($past(m_valid && !m_ready))
                assert (m_valid && $stable({m_instrument,offered}));
            if (m_valid) begin
                assert (int'(m_instrument) < INSTRUMENTS);
                if (changing_banks[held_at] || replaced[held_at])
                    assert (!m_current);
            end
            if (m_current) begin
                assert (m_bid_size == live_bid_size[QTY_W*held_at+:QTY_W]);
                assert (m_ask_size == live_ask_size[QTY_W*held_at+:QTY_W]);
            end
            cover (m_valid && m_current && m_bid_size > 0 && m_ask_size > 0);
            cover (m_valid && m_current && (m_bid_size == 0) != (m_ask_size == 0));
            cover (m_valid && !m_current && changing_banks[held_at]);
        end
    end
`endif

endmodule

`default_nettype wire
