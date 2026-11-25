`timescale 1ns/1ps
`include "quote_sweep_codes.svh"
`default_nettype none

// The route planner computes against free inventory. Before publishing, its
// selected bundle is revalidated and converted into one bounded reservation.
module quote_reserved_sweep #(
    parameter integer INSTRUMENTS = 4,
    parameter integer VENUES = 4,
    parameter integer PRICE_W = 20,
    parameter integer QTY_W = 14,
    parameter integer TIME_W = 16,
    parameter integer SEQ_W = 12,
    parameter integer TAG_W = 16,
    parameter integer FEE_W = 12,
    parameter integer EPOCH_W = 16,
    parameter integer RES_SLOTS = 8,
    parameter integer REV_W = 32,
    parameter integer TOKEN_W = 16,
    localparam integer INST_W = $clog2(INSTRUMENTS+1),
    localparam integer VENUE_W = $clog2(VENUES+1),
    localparam integer REQUEST_W = QTY_W + $clog2(VENUES),
    localparam integer UNIT_W = (PRICE_W+1 > FEE_W ? PRICE_W+1 : FEE_W) + 1,
    localparam integer VALUE_W = UNIT_W + REQUEST_W,
    localparam integer RECORD_W = 2*(PRICE_W+QTY_W) + TIME_W + SEQ_W,
    localparam integer CHILD_W = VENUE_W + PRICE_W + QTY_W + FEE_W + TIME_W + SEQ_W,
    localparam integer SLOT_W = $clog2(RES_SLOTS+1),
    localparam integer SUM_W = QTY_W + SLOT_W
) (
    input wire clk, rst,
    input wire [TIME_W-1:0] now_tick,

    input wire cmd_valid,
    output wire cmd_ready,
    input wire [2:0] cmd_op,
    input wire [INST_W-1:0] cmd_instrument,
    input wire [VENUE_W-1:0] cmd_venue,
    input wire [SEQ_W-1:0] cmd_sequence,
    input wire [PRICE_W-1:0] cmd_bid_price, cmd_ask_price,
    input wire [QTY_W-1:0] cmd_bid_qty, cmd_ask_qty,
    input wire [TAG_W-1:0] cmd_tag,
    output wire ack_valid,
    input wire ack_ready,
    output wire [3:0] ack_code,
    output wire [TAG_W-1:0] ack_tag,

    input wire cfg_valid,
    output wire cfg_ready,
    input wire [VENUE_W-1:0] cfg_venue,
    input wire cfg_enabled,
    input wire [QTY_W-1:0] cfg_cap,
    input wire signed [FEE_W-1:0] cfg_fee,
    output wire cfg_error,
    input wire commit_valid,
    output wire commit_ready,
    output wire [EPOCH_W-1:0] policy_epoch,

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
    input wire [TIME_W-1:0] query_ttl,

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
    output logic [VENUES*CHILD_W-1:0] result_children,
    output logic [2:0] result_reservation,
    output logic [SLOT_W-1:0] result_slot,
    output logic [TOKEN_W-1:0] result_generation,
    output logic [TIME_W-1:0] result_expires,

    input wire action_valid,
    output wire action_ready,
    input wire action_claim,
    input wire [TAG_W-1:0] action_tag,
    input wire [SLOT_W-1:0] action_slot,
    input wire [TOKEN_W-1:0] action_generation,
    output wire grant_valid,
    input wire grant_ready,
    output wire grant_applicable,
    output wire [2:0] grant_status,
    output wire grant_claim,
    output wire [TAG_W-1:0] grant_tag,
    output wire [SLOT_W-1:0] grant_slot,
    output wire [TOKEN_W-1:0] grant_generation,
    output wire [INST_W-1:0] grant_instrument,
    output wire grant_buy,
    output wire [VENUES*CHILD_W-1:0] grant_children,

    output wire [SLOT_W-1:0] reservation_count,
    output wire [31:0] reserved_total, dispatched_total, released_total,
    output wire [31:0] invalidated_total, revoked_grants
);
    localparam logic [2:0] HELD = 0, NO_PLAN = 1, QUOTE_CHANGED = 2,
                           POLICY_CHANGED = 3, NO_TOKEN = 4, BAD_TTL = 5;
    localparam integer CHILD_QTY_LOW = FEE_W + TIME_W + SEQ_W;

    wire mutation;
    wire policy_change = commit_valid && commit_ready;
    wire [VENUES-1:0] read_enabled, read_present, policy_enabled, poisoned;
    wire [VENUES*RECORD_W-1:0] raw_records, read_records;
    wire [VENUES*QTY_W-1:0] policy_caps, consumed;
    wire [VENUES*FEE_W-1:0] policy_fees;
    wire [VENUES*TIME_W-1:0] quote_updates;
    wire [VENUES*REV_W-1:0] revisions;
    wire [VENUES*SUM_W-1:0] held_quantities;
    wire [VENUES*QTY_W-1:0] dispatch_amounts;
    wire dispatch;
    logic inflight, policy_dirty;
    logic [INST_W-1:0] query_row;
    logic query_side;
    logic [TIME_W-1:0] saved_age, saved_ttl;
    logic [VENUES*TIME_W-1:0] saved_updates;
    logic [VENUES*REV_W-1:0] saved_revisions;
    wire [INST_W-1:0] read_instrument = inflight ? query_row : query_instrument;
    wire read_buy = inflight ? query_side : query_buy;
    wire planner_ready, planner_valid;
    wire [3:0] plan_status;
    wire [TAG_W-1:0] plan_tag;
    wire [TIME_W-1:0] plan_tick;
    wire [EPOCH_W-1:0] plan_epoch;
    wire [VENUES-1:0] plan_eligible, plan_stale;
    wire [VENUE_W-1:0] plan_count;
    wire [REQUEST_W-1:0] plan_quantity;
    wire signed [VALUE_W-1:0] plan_value;
    wire [VENUES*CHILD_W-1:0] plan_children;
    wire book_space;
    wire [SLOT_W-1:0] book_slot;
    wire [TOKEN_W-1:0] book_generation;
    wire [VENUES*QTY_W-1:0] picked_amounts;
    wire [VENUES-1:0] expired_or_changed;
    wire allow_query = !inflight && (!result_valid || result_ready);
    wire query_fire = query_valid && query_ready;
    wire plan_done = planner_valid && inflight;
    logic [2:0] reservation_status;

    assign query_ready = allow_query && planner_ready;

    quote_store_engine #(
        .INSTRUMENTS(INSTRUMENTS), .VENUES(VENUES), .PRICE_W(PRICE_W),
        .QTY_W(QTY_W), .TIME_W(TIME_W), .SEQ_W(SEQ_W), .TAG_W(TAG_W)
    ) store (
        .read_records(raw_records), .mutation_commit(mutation), .*
    );

    quote_fee_policy #(.VENUES(VENUES), .QTY_W(QTY_W), .FEE_W(FEE_W), .EPOCH_W(EPOCH_W))
        policy (.*);

    quote_inventory #(.INSTRUMENTS(INSTRUMENTS), .VENUES(VENUES), .QTY_W(QTY_W), .REV_W(REV_W))
        inventory (
            .mutation_instrument(cmd_instrument), .mutation_venue(cmd_venue),
            .dispatch_instrument(grant_instrument), .dispatch_buy(grant_buy), .*
        );

    // Subtract both held and consumed claims at the snapshot. A true quote
    // mutation can change a child even if the modular source serial wraps.
    for (genvar venue = 0; venue < VENUES; venue++) begin : g_venue
        wire [SEQ_W-1:0] serial_number;
        wire [TIME_W-1:0] update_tick;
        wire [QTY_W-1:0] bid_qty, ask_qty;
        wire [PRICE_W-1:0] bid_price, ask_price;
        wire [QTY_W-1:0] full_qty = read_buy ? ask_qty : bid_qty;
        wire [SUM_W:0] unavailable =
            (SUM_W+1)'(held_quantities[venue*SUM_W +: SUM_W]) +
            (SUM_W+1)'(consumed[venue*QTY_W +: QTY_W]);
        wire [QTY_W-1:0] free_qty = !poisoned[venue] && unavailable < (SUM_W+1)'(full_qty)
            ? full_qty - QTY_W'(unavailable) : '0;
        wire [TIME_W-1:0] age = now_tick - saved_updates[venue*TIME_W +: TIME_W];
        logic [QTY_W-1:0] selected;

        assign {serial_number,update_tick,bid_qty,ask_qty,bid_price,ask_price}
            = raw_records[venue*RECORD_W +: RECORD_W];
        assign quote_updates[venue*TIME_W +: TIME_W] = update_tick;
        assign read_records[venue*RECORD_W +: RECORD_W] = {
            serial_number,update_tick,read_buy ? bid_qty : free_qty,read_buy ? free_qty : ask_qty,
            bid_price,ask_price
        };

        always @* begin
            selected = '0;
            for (integer index = 0; index < VENUES; index++) begin
                if (index < int'(plan_count) &&
                    plan_children[index*CHILD_W+CHILD_W-1 -: VENUE_W] == VENUE_W'(venue))
                    selected = plan_children[index*CHILD_W+CHILD_QTY_LOW +: QTY_W];
            end
        end
        assign picked_amounts[venue*QTY_W +: QTY_W] = selected;
        assign expired_or_changed[venue] = selected != 0 && (poisoned[venue] || age[TIME_W-1] ||
            age > saved_age || revisions[venue*REV_W +: REV_W] != saved_revisions[venue*REV_W +: REV_W] ||
            (mutation && cmd_instrument == query_row && cmd_venue == VENUE_W'(venue)));
    end

    quote_sweep_plan #(
        .INSTRUMENTS(INSTRUMENTS), .VENUES(VENUES), .PRICE_W(PRICE_W),
        .QTY_W(QTY_W), .TIME_W(TIME_W), .SEQ_W(SEQ_W), .TAG_W(TAG_W),
        .FEE_W(FEE_W), .EPOCH_W(EPOCH_W)
    ) plan (
        .query_valid(query_valid && allow_query), .query_ready(planner_ready),
        .result_valid(planner_valid), .result_ready(inflight),
        .result_status(plan_status), .result_tag(plan_tag), .result_tick(plan_tick),
        .result_epoch(plan_epoch), .result_eligible(plan_eligible), .result_stale(plan_stale),
        .result_count(plan_count), .result_quantity(plan_quantity), .result_value(plan_value),
        .result_children(plan_children), .*
    );

    always @* begin
        reservation_status = HELD;
        if (plan_status >= `SWEEP_BAD_INST)
            reservation_status = NO_PLAN;
        else if (saved_ttl == 0 || saved_ttl[TIME_W-1])
            reservation_status = BAD_TTL;
        else if (plan_count == 0)
            reservation_status = NO_PLAN;
        else if (policy_dirty || policy_change)
            reservation_status = POLICY_CHANGED;
        else if (|expired_or_changed)
            reservation_status = QUOTE_CHANGED;
        else if (!book_space)
            reservation_status = NO_TOKEN;
    end

    quote_reservation_ledger #(
        .INSTRUMENTS(INSTRUMENTS), .VENUES(VENUES), .SLOTS(RES_SLOTS), .QTY_W(QTY_W),
        .TIME_W(TIME_W), .TAG_W(TAG_W), .TOKEN_W(TOKEN_W), .CHILD_W(CHILD_W)
    ) reservations (
        .mutation_instrument(cmd_instrument), .mutation_venue(cmd_venue),
        .book(plan_done && reservation_status == HELD), .book_instrument(query_row),
        .book_buy(query_side), .book_ttl(saved_ttl), .book_max_age(saved_age),
        .book_updates(saved_updates), .book_amounts(picked_amounts), .book_children(plan_children),
        .*
    );

    always_ff @(posedge clk) begin
        if (rst) begin
            inflight <= 1'b0;
            policy_dirty <= 1'b0;
            result_valid <= 1'b0;
        end else begin
            if (result_ready)
                result_valid <= 1'b0;
            if (query_fire) begin
                inflight <= 1'b1;
                policy_dirty <= policy_change;
                query_row <= query_instrument;
                query_side <= query_buy;
                saved_age <= query_max_age;
                saved_ttl <= query_ttl;
                saved_updates <= quote_updates;
                saved_revisions <= revisions;
            end else if (plan_done) begin
                inflight <= 1'b0;
                result_valid <= 1'b1;
                result_reservation <= reservation_status;
                result_status <= plan_status;
                result_tag <= plan_tag;
                result_tick <= plan_tick;
                result_epoch <= plan_epoch;
                result_eligible <= plan_eligible;
                result_stale <= plan_stale;
                result_slot <= reservation_status == HELD ? book_slot : '0;
                result_generation <= reservation_status == HELD ? book_generation : '0;
                result_expires <= reservation_status == HELD ? now_tick+saved_ttl : '0;
                result_count <= reservation_status == HELD ? plan_count : '0;
                result_quantity <= reservation_status == HELD ? plan_quantity : '0;
                result_value <= reservation_status == HELD ? plan_value : '0;
                result_children <= reservation_status == HELD ? plan_children : '0;
            end else if (inflight && policy_change)
                policy_dirty <= 1'b1;
        end
    end
endmodule

`default_nettype wire
