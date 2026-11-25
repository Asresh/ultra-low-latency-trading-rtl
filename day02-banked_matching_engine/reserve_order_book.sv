`timescale 1ns/1ps
`default_nettype none
`include "reserve_codes.svh"

// Single-clock reserve book. Dense age ranks keep priority exact with no
// deadline on how long an order can remain live.
module reserve_order_book #(
    parameter integer PRICE_LEVELS = 12,
    parameter integer ORDER_SLOTS = 12,
    parameter integer QTY_W = 16,
    parameter integer OWNER_W = 8,
    localparam integer PRICE_W = $clog2(PRICE_LEVELS + 1),
    localparam integer ID_W = $clog2(ORDER_SLOTS + 1),
    localparam integer SUM_W = QTY_W + ID_W,
    localparam integer COMMAND_W = 8 + ID_W + PRICE_W + 3*QTY_W + OWNER_W,
    localparam integer RESULT_W = 11 + ID_W + PRICE_W + 5*QTY_W
                                + OWNER_W + 2*SUM_W + ID_W
) (
    input  wire                  clk,
    input  wire                  rst,
    input  wire                  s_valid,
    output wire                  s_ready,
    input  wire [COMMAND_W-1:0]  s_command,
    output wire                  m_valid,
    input  wire                  m_ready,
    output wire [RESULT_W-1:0]   m_result
);
    localparam integer ADDR_W = ORDER_SLOTS > 1 ? $clog2(ORDER_SLOTS) : 1;
    logic [ORDER_SLOTS-1:0] live;
    logic [ID_W-1:0] live_count;
    logic [ID_W-1:0] age [0:ORDER_SLOTS-1];
    logic side [0:ORDER_SLOTS-1];
    logic [PRICE_W-1:0] price [0:ORDER_SLOTS-1];
    logic [QTY_W-1:0] total [0:ORDER_SLOTS-1];
    logic [QTY_W-1:0] visible [0:ORDER_SLOTS-1];
    logic [QTY_W-1:0] peak [0:ORDER_SLOTS-1];
    logic [OWNER_W-1:0] owner [0:ORDER_SLOTS-1];
    logic import_open;
    logic [ID_W-1:0] staged_count, staged_expected, snapshot_rank;
    logic [ORDER_SLOTS-1:0] staged_live;
    logic [ID_W-1:0] staged_age [0:ORDER_SLOTS-1];
    logic staged_side [0:ORDER_SLOTS-1];
    logic [PRICE_W-1:0] staged_price [0:ORDER_SLOTS-1];
    logic [QTY_W-1:0] staged_total [0:ORDER_SLOTS-1];
    logic [QTY_W-1:0] staged_visible [0:ORDER_SLOTS-1];
    logic [QTY_W-1:0] staged_peak [0:ORDER_SLOTS-1];
    logic [OWNER_W-1:0] staged_owner [0:ORDER_SLOTS-1];

    logic active, first_event;
    logic [2:0] operation;
    logic [1:0] tif, stp;
    logic command_side;
    logic [ID_W-1:0] id;
    logic [PRICE_W-1:0] limit_price;
    logic [QTY_W-1:0] remaining, display_peak, import_visible;
    logic [OWNER_W-1:0] account;
    wire [ADDR_W-1:0] slot = ADDR_W'(id);

    logic have_maker;
    logic [ADDR_W-1:0] maker;
    logic [SUM_W-1:0] crossing_depth;
    logic [SUM_W-1:0] level_total, level_visible;
    logic [ID_W-1:0] level_orders;
    logic staged_crosses;
    logic [ADDR_W-1:0] snapshot_slot;

    function automatic logic crosses_limit(
        input logic taker_side,
        input logic [PRICE_W-1:0] limit,
        input logic maker_side,
        input logic [PRICE_W-1:0] maker_price
    );
        crosses_limit = (maker_side != taker_side) &&
            (taker_side ? maker_price >= limit : maker_price <= limit);
    endfunction

    function automatic logic [QTY_W-1:0] open_slice(
        input logic [QTY_W-1:0] balance, maximum_display
    );
        open_slice = maximum_display == '0 || balance < maximum_display
            ? balance : maximum_display;
    endfunction

    // Price prevails before age. FOK accumulates total, so hidden reserve
    // participates in preflight and is exposed in slices during execution.
    always @* begin : scan_live_orders
        logic better_price;
        have_maker = 1'b0;
        maker = '0;
        crossing_depth = '0;
        level_total = '0;
        level_visible = '0;
        level_orders = '0;
        staged_crosses = 1'b0;
        snapshot_slot = '0;
        for (integer i = 0; i < ORDER_SLOTS; i = i + 1) begin
            better_price = command_side ? price[i] > price[maker] : price[i] < price[maker];
            if (live[i]) begin
                if (age[i] == snapshot_rank)
                    snapshot_slot = ADDR_W'(i);
                if (side[i] == command_side && price[i] == limit_price) begin
                    level_total = level_total + SUM_W'(total[i]);
                    level_visible = level_visible + SUM_W'(visible[i]);
                    level_orders = level_orders + 1'b1;
                end
                if (crosses_limit(command_side, limit_price, side[i], price[i])) begin
                    if (stp != `RM_STP_MAKER || owner[i] != account)
                        crossing_depth = crossing_depth + SUM_W'(total[i]);
                    if (!have_maker || better_price ||
                        (price[i] == price[maker] && age[i] < age[maker])) begin
                        have_maker = 1'b1;
                        maker = ADDR_W'(i);
                    end
                end
            end
            if (staged_live[i] &&
                crosses_limit(command_side, limit_price, staged_side[i], staged_price[i]))
                staged_crosses = 1'b1;
        end
    end

    wire self_match = owner[maker] == account && stp != `RM_STP_NONE;
    wire [QTY_W-1:0] fill_size = remaining < visible[maker] ? remaining : visible[maker];
    wire [QTY_W-1:0] maker_total_after = total[maker] - fill_size;
    wire [QTY_W-1:0] slice_after = visible[maker] - fill_size;
    wire refresh = slice_after == '0 && maker_total_after != '0;

    logic [3:0] event_kind, reason;
    logic event_last, event_side, event_refresh;
    logic [ID_W-1:0] event_maker, event_count;
    logic [PRICE_W-1:0] event_price;
    logic [QTY_W-1:0] event_qty, event_remaining, event_total, event_visible, event_peak;
    logic [OWNER_W-1:0] event_owner;
    logic [SUM_W-1:0] event_level_total, event_level_visible;

    assign s_ready = !rst && !active;
    assign m_valid = !rst && active;
    assign m_result = {event_kind, event_last, event_side, event_maker, event_price, event_qty,
                       event_remaining, event_total, event_visible, event_peak, event_owner,
                       event_refresh, event_level_total, event_level_visible, event_count, reason};

    // The command queue is independent from the event sink. State only
    // changes on an event transfer, which keeps held reports immutable.
    always @* begin : form_event
        event_kind = `RM_EV_REJECT;
        reason = '0;
        event_last = 1'b1;
        event_side = 1'b0;
        event_refresh = 1'b0;
        event_maker = '0;
        event_count = '0;
        event_price = '0;
        event_qty = '0;
        event_remaining = '0;
        event_total = '0;
        event_visible = '0;
        event_peak = '0;
        event_owner = '0;
        event_level_total = '0;
        event_level_visible = '0;

        if (operation > `RM_IMPORT) begin
            reason = `RM_ERR_OPCODE;
        end else if (operation == `RM_SNAPSHOT) begin
            event_kind = `RM_EV_SNAPSHOT;
            event_count = snapshot_rank;
            if (snapshot_rank != live_count) begin
                event_last = 1'b0;
                event_side = side[snapshot_slot];
                event_maker = ID_W'(snapshot_slot);
                event_price = price[snapshot_slot];
                event_total = total[snapshot_slot];
                event_visible = visible[snapshot_slot];
                event_peak = peak[snapshot_slot];
                event_owner = owner[snapshot_slot];
            end
        end else if (operation == `RM_IMPORT) begin
            case (tif)
                `RM_IMPORT_BEGIN: begin
                    if (import_open) reason = `RM_ERR_IMPORT;
                    else if (id > ID_W'(ORDER_SLOTS)) reason = `RM_ERR_ID;
                    else event_kind = `RM_EV_IMPORT;
                end
                `RM_IMPORT_ROW: begin
                    if (!import_open || staged_count >= staged_expected)
                        reason = `RM_ERR_IMPORT;
                    else if (id >= ID_W'(ORDER_SLOTS))
                        reason = `RM_ERR_ID;
                    else if (staged_live[slot])
                        reason = `RM_ERR_DUPLICATE;
                    else if (limit_price >= PRICE_W'(PRICE_LEVELS))
                        reason = `RM_ERR_PRICE;
                    else if (remaining == '0)
                        reason = `RM_ERR_QTY;
                    else if (import_visible == '0 || import_visible > remaining ||
                             (display_peak == '0 ? import_visible != remaining
                                                 : import_visible > display_peak))
                        reason = `RM_ERR_DISPLAY;
                    else if (staged_crosses)
                        reason = `RM_ERR_CROSSED;
                    else begin
                        event_kind = `RM_EV_IMPORT;
                        event_count = staged_count + 1'b1;
                        event_side = command_side;
                        event_maker = id;
                        event_price = limit_price;
                        event_total = remaining;
                        event_visible = import_visible;
                        event_peak = display_peak;
                        event_owner = account;
                    end
                end
                default: begin
                    if (!import_open || (tif == `RM_IMPORT_COMMIT && staged_count != staged_expected))
                        reason = `RM_ERR_IMPORT;
                    else begin
                        event_kind = `RM_EV_IMPORT;
                        event_count = staged_count;
                    end
                end
            endcase
        end else if (import_open && operation != `RM_LEVEL && operation != `RM_ORDER) begin
            reason = `RM_ERR_IMPORT;
        end else if (operation == `RM_LEVEL) begin
            if (limit_price >= PRICE_W'(PRICE_LEVELS)) begin
                reason = `RM_ERR_PRICE;
            end else begin
                event_kind = `RM_EV_LEVEL;
                event_side = command_side;
                event_price = limit_price;
                event_level_total = level_total;
                event_level_visible = level_visible;
                event_count = level_orders;
            end
        end else if (id >= ID_W'(ORDER_SLOTS)) begin
            reason = `RM_ERR_ID;
        end else if (operation != `RM_NEW) begin
            if (!live[slot])
                reason = `RM_ERR_ABSENT;
            else if (owner[slot] != account)
                reason = `RM_ERR_OWNER;
            else if (operation == `RM_REDUCE && (remaining == '0 || remaining >= total[slot]))
                reason = `RM_ERR_REDUCE;
            else begin
                event_side = side[slot];
                event_price = price[slot];
                event_owner = owner[slot];
                if (operation == `RM_CANCEL) begin
                    event_kind = `RM_EV_CANCEL;
                    event_qty = total[slot];
                end else begin
                    event_peak = peak[slot];
                    if (operation == `RM_ORDER) begin
                        event_kind = `RM_EV_ORDER;
                        event_total = total[slot];
                        event_visible = visible[slot];
                    end else begin
                        event_kind = `RM_EV_REDUCE;
                        event_qty = total[slot] - remaining;
                        event_total = remaining;
                        // Cancel the hidden shares before reducing displayed liquidity.
                        event_visible = visible[slot] < remaining ? visible[slot] : remaining;
                    end
                end
            end
        end else if (limit_price >= PRICE_W'(PRICE_LEVELS)) begin
            reason = `RM_ERR_PRICE;
        end else if (remaining == '0) begin
            reason = `RM_ERR_QTY;
        end else if (stp > `RM_STP_TAKER || (tif == `RM_FOK && stp == `RM_STP_TAKER)) begin
            reason = `RM_ERR_POLICY;
        end else if (first_event && display_peak > remaining) begin
            reason = `RM_ERR_DISPLAY;
        end else if (live[slot]) begin
            reason = `RM_ERR_DUPLICATE;
        end else if (have_maker && tif == `RM_POST_ONLY) begin
            reason = `RM_ERR_POST;
        end else if (first_event && tif == `RM_FOK && crossing_depth < SUM_W'(remaining)) begin
            reason = `RM_ERR_FOK;
        end else if (have_maker) begin
            event_side = command_side;
            event_price = price[maker];
            event_maker = ID_W'(maker);
            event_owner = owner[maker];
            if (self_match && stp == `RM_STP_TAKER) begin
                event_kind = `RM_EV_STP_TAKER;
                event_qty = remaining;
                event_total = total[maker];
                event_visible = visible[maker];
                event_peak = peak[maker];
            end else if (self_match) begin
                event_kind = `RM_EV_STP_MAKER;
                event_qty = total[maker];
                event_remaining = remaining;
                event_last = 1'b0;
            end else begin
                event_kind = `RM_EV_FILL;
                event_qty = fill_size;
                event_remaining = remaining - fill_size;
                event_last = remaining == fill_size;
                event_total = maker_total_after;
                event_visible = refresh ? open_slice(maker_total_after, peak[maker]) : slice_after;
                event_peak = maker_total_after != '0 ? peak[maker] : QTY_W'(0);
                event_refresh = refresh;
            end
        end else begin
            event_kind = tif == `RM_GTC || tif == `RM_POST_ONLY ? `RM_EV_REST : `RM_EV_EXPIRE;
            event_side = command_side;
            event_price = limit_price;
            event_qty = remaining;
            event_owner = account;
            if (event_kind == `RM_EV_REST) begin
                event_remaining = remaining;
                event_total = remaining;
                event_visible = open_slice(remaining, display_peak);
                event_peak = display_peak;
            end
        end
    end

    wire [ADDR_W-1:0] current = event_kind == `RM_EV_CANCEL ? slot : maker;
    wire retire_order = event_kind == `RM_EV_CANCEL || event_kind == `RM_EV_STP_MAKER ||
                        (event_kind == `RM_EV_FILL && event_total == '0);

    always_ff @(posedge clk) begin
        if (rst) begin
            active <= 1'b0;
            first_event <= 1'b0;
            live <= '0;
            live_count <= '0;
            import_open <= 1'b0;
            staged_live <= '0;
            staged_count <= '0;
            staged_expected <= '0;
            snapshot_rank <= '0;
            {operation, tif, stp, command_side, id, limit_price,
             remaining, display_peak, import_visible, account} <= '0;
        end else if (s_valid && s_ready) begin
            active <= 1'b1;
            first_event <= 1'b1;
            snapshot_rank <= '0;
            {operation, tif, stp, command_side, id, limit_price,
             remaining, display_peak, import_visible, account} <= s_command;
        end else if (m_valid && m_ready) begin
            active <= !event_last;
            first_event <= 1'b0;
            remaining <= event_remaining;
            if (event_kind == `RM_EV_SNAPSHOT && !event_last)
                snapshot_rank <= snapshot_rank + 1'b1;
            if (event_kind == `RM_EV_IMPORT) begin
                case (tif)
                    `RM_IMPORT_BEGIN: begin
                        import_open <= 1'b1;
                        staged_live <= '0;
                        staged_count <= '0;
                        staged_expected <= id;
                    end
                    `RM_IMPORT_ROW: begin
                        staged_live[slot] <= 1'b1;
                        staged_count <= staged_count + 1'b1;
                        staged_age[slot] <= staged_count;
                        staged_side[slot] <= command_side;
                        staged_price[slot] <= limit_price;
                        staged_total[slot] <= remaining;
                        staged_visible[slot] <= import_visible;
                        staged_peak[slot] <= display_peak;
                        staged_owner[slot] <= account;
                    end
                    default: begin
                        import_open <= 1'b0;
                        staged_live <= '0;
                        staged_count <= '0;
                        if (tif == `RM_IMPORT_COMMIT) begin
                            live <= staged_live;
                            live_count <= staged_count;
                            for (integer i = 0; i < ORDER_SLOTS; i = i + 1) begin
                                if (staged_live[i]) begin
                                    age[i] <= staged_age[i];
                                    side[i] <= staged_side[i];
                                    price[i] <= staged_price[i];
                                    total[i] <= staged_total[i];
                                    visible[i] <= staged_visible[i];
                                    peak[i] <= staged_peak[i];
                                    owner[i] <= staged_owner[i];
                                end
                            end
                        end
                    end
                endcase
            end
            if (retire_order || event_refresh) begin
                for (integer i = 0; i < ORDER_SLOTS; i = i + 1)
                    if (live[i] && age[i] > age[current])
                        age[i] <= age[i] - 1'b1;
            end
            if (retire_order) begin
                live[current] <= 1'b0;
                live_count <= live_count - 1'b1;
            end else if (event_kind == `RM_EV_FILL) begin
                total[maker] <= event_total;
                visible[maker] <= event_visible;
                if (event_refresh)
                    age[maker] <= live_count - 1'b1;
            end else if (event_kind == `RM_EV_REDUCE) begin
                total[slot] <= event_total;
                visible[slot] <= event_visible;
            end else if (event_kind == `RM_EV_REST) begin
                live[slot] <= 1'b1;
                live_count <= live_count + 1'b1;
                age[slot] <= live_count;
                price[slot] <= limit_price;
                side[slot] <= command_side;
                total[slot] <= event_total;
                visible[slot] <= event_visible;
                peak[slot] <= display_peak;
                owner[slot] <= account;
            end
        end
    end

`ifdef FORMAL
`include "reserve_properties.svh"
`endif
endmodule

`default_nettype wire
