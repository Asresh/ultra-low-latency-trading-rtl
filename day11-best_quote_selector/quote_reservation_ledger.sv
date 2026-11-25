`timescale 1ns/1ps
`default_nettype none

// The table owns held quantities and a separate grant mailbox. An execution
// adapter consumes quote credit only on a still-applicable grant transfer.
module quote_reservation_ledger #(
    parameter integer INSTRUMENTS = 4,
    parameter integer VENUES = 4,
    parameter integer SLOTS = 8,
    parameter integer QTY_W = 14,
    parameter integer TIME_W = 16,
    parameter integer TAG_W = 16,
    parameter integer TOKEN_W = 16,
    parameter integer CHILD_W = 80,
    localparam integer INST_W = $clog2(INSTRUMENTS+1),
    localparam integer VENUE_W = $clog2(VENUES+1),
    localparam integer SLOT_W = $clog2(SLOTS+1),
    localparam integer SUM_W = QTY_W + $clog2(SLOTS+1)
) (
    input wire clk, rst,
    input wire [TIME_W-1:0] now_tick,
    input wire mutation,
    input wire [INST_W-1:0] mutation_instrument,
    input wire [VENUE_W-1:0] mutation_venue,
    input wire policy_change,

    input wire [INST_W-1:0] read_instrument,
    input wire read_buy,
    output logic [VENUES*SUM_W-1:0] held_quantities,

    input wire book,
    input wire [INST_W-1:0] book_instrument,
    input wire book_buy,
    input wire [TIME_W-1:0] book_max_age, book_ttl,
    input wire [VENUES*TIME_W-1:0] book_updates,
    input wire [VENUES*QTY_W-1:0] book_amounts,
    input wire [VENUES*CHILD_W-1:0] book_children,
    output logic book_space,
    output logic [SLOT_W-1:0] book_slot,
    output wire [TOKEN_W-1:0] book_generation,

    input wire action_valid,
    output wire action_ready,
    input wire action_claim,
    input wire [TAG_W-1:0] action_tag,
    input wire [SLOT_W-1:0] action_slot,
    input wire [TOKEN_W-1:0] action_generation,

    output wire grant_valid,
    input wire grant_ready,
    output wire grant_applicable,
    output logic [2:0] grant_status,
    output logic grant_claim,
    output logic [TAG_W-1:0] grant_tag,
    output logic [SLOT_W-1:0] grant_slot,
    output logic [TOKEN_W-1:0] grant_generation,
    output logic [INST_W-1:0] grant_instrument,
    output logic grant_buy,
    output logic [VENUES*CHILD_W-1:0] grant_children,

    output wire dispatch,
    output wire [VENUES*QTY_W-1:0] dispatch_amounts,
    output logic [SLOT_W-1:0] reservation_count,
    output logic [31:0] reserved_total, dispatched_total, released_total,
    output logic [31:0] invalidated_total, revoked_grants
);
    localparam logic [2:0] OK = 0, BAD_ADDRESS = 1, WRONG_GENERATION = 2,
                           NOT_HELD = 3, REVOKED = 4;
    localparam integer INDEX_W = SLOTS > 1 ? $clog2(SLOTS) : 1;
    localparam integer VENUE_INDEX_W = VENUES > 1 ? $clog2(VENUES) : 1;
    localparam integer AMOUNTS_W = VENUES*QTY_W, UPDATES_W = VENUES*TIME_W, CHILDREN_W = VENUES*CHILD_W;
    localparam integer TOKEN_RECORD_W = 1 + TOKEN_W + INST_W + 1 +
        3*TIME_W + AMOUNTS_W + UPDATES_W + CHILDREN_W;
    typedef struct packed {
        logic active;
        logic [TOKEN_W-1:0] generation;
        logic [INST_W-1:0] instrument;
        logic buy;
        logic [TIME_W-1:0] birth, ttl, age_limit;
        logic [AMOUNTS_W-1:0] amounts;
        logic [UPDATES_W-1:0] updates;
        logic [CHILDREN_W-1:0] children;
    } token_t;

    wire [TOKEN_RECORD_W-1:0] tokens [0:SLOTS-1];
    wire [SLOTS-1:0] alive, retiring, capacity;
    wire [SLOTS-1:0] read_matches;
    wire [AMOUNTS_W-1:0] slot_amounts [0:SLOTS-1];
    wire [TOKEN_W-1:0] generations [0:SLOTS-1];

    // Views project only the fields needed by the two interfaces.
    /* verilator lint_off UNUSEDSIGNAL */
    token_t queried, granted;
    /* verilator lint_on UNUSEDSIGNAL */
    logic grant_pending;
    logic [2:0] outcome;
    logic [INDEX_W-1:0] cursor;
    wire [INDEX_W-1:0] action_index = SLOTS == 1 ? '0 : INDEX_W'(action_slot);
    wire [INDEX_W-1:0] grant_index = SLOTS == 1 ? '0 : INDEX_W'(grant_slot);
    wire valid_slot = int'(action_slot) < SLOTS;
    wire action_fire = action_valid && action_ready;
    wire grant_fire = grant_valid && grant_ready;
    wire release_fire = action_fire && !action_claim && outcome == OK;
    wire [VENUE_INDEX_W-1:0] change_column = VENUES == 1 ? '0 : VENUE_INDEX_W'(mutation_venue);

    assign queried = tokens[action_index];
    assign granted = tokens[grant_index];
    assign action_ready = !rst && !grant_pending;
    assign grant_valid = !rst && grant_pending;
    assign grant_applicable = grant_valid && grant_claim && grant_status == OK &&
                              alive[grant_index] && granted.generation == grant_generation;
    assign dispatch = grant_fire && grant_applicable;
    assign dispatch_amounts = dispatch ? granted.amounts : '0;
    assign book_generation = book_space ? generations[INDEX_W'(book_slot)] + 1'b1 : '0;

    always @* begin
        outcome = OK;
        if (!valid_slot)
            outcome = BAD_ADDRESS;
        else if (action_generation == 0 || action_generation != queried.generation)
            outcome = WRONG_GENERATION;
        else if (!queried.active)
            outcome = NOT_HELD;
        else if (!alive[action_index])
            outcome = REVOKED;
    end

    for (genvar slot = 0; slot < SLOTS; slot++) begin : g_tokens
        token_t saved;
        wire [TIME_W-1:0] elapsed = now_tick - saved.birth;
        wire ttl_ended = elapsed[TIME_W-1] || elapsed >= saved.ttl;
        wire [VENUES-1:0] too_old;
        wire affected = mutation && int'(mutation_venue) < VENUES &&
                        saved.instrument == mutation_instrument &&
            slot_amounts[slot][QTY_W*change_column +: QTY_W] != 0;
        assign tokens[slot] = saved;
        assign slot_amounts[slot] = saved.amounts;
        assign generations[slot] = saved.generation;
        assign alive[slot] = saved.active && !ttl_ended && !(|too_old) && !policy_change && !affected;
        assign retiring[slot] = saved.active && !alive[slot];
        assign capacity[slot] = !alive[slot] && !(&saved.generation);
        assign read_matches[slot] = alive[slot] && saved.instrument == read_instrument &&
                                    saved.buy == read_buy;

        for (genvar venue = 0; venue < VENUES; venue++) begin : g_age
            wire [TIME_W-1:0] age = now_tick - saved.updates[venue*TIME_W +: TIME_W];
            assign too_old[venue] = saved.amounts[venue*QTY_W +: QTY_W] != 0 &&
                                     (age[TIME_W-1] || age > saved.age_limit);
        end

        always_ff @(posedge clk) begin
            if (rst)
                saved <= '0;
            else if (book && book_space && book_slot == SLOT_W'(slot))
                saved <= {1'b1,book_generation,book_instrument,book_buy,now_tick,
                          book_ttl,book_max_age,book_amounts,book_updates,book_children};
            else if (retiring[slot] || (release_fire && action_slot == SLOT_W'(slot)) ||
                     (dispatch && grant_slot == SLOT_W'(slot)))
                saved.active <= 1'b0;
        end
    end

    for (genvar venue = 0; venue < VENUES; venue++) begin : g_accumulate
        always @* begin
            logic [SUM_W-1:0] total;
            total = '0;
            for (integer slot = 0; slot < SLOTS; slot++)
                if (read_matches[slot])
                    total = total + SUM_W'(slot_amounts[slot][venue*QTY_W +: QTY_W]);
            held_quantities[venue*SUM_W +: SUM_W] = total;
        end
    end

    always @* begin : select_slot
        integer candidate;
        book_space = 1'b0;
        book_slot = '0;
        reservation_count = '0;
        for (integer offset = 0; offset < SLOTS; offset++) begin
            candidate = int'(cursor) + offset;
            if (candidate >= SLOTS)
                candidate = candidate - SLOTS;
            if (!book_space && capacity[candidate]) begin
                book_space = 1'b1;
                book_slot = SLOT_W'(candidate);
            end
            reservation_count = reservation_count + SLOT_W'(alive[offset]);
        end
    end

    logic [31:0] invalidating;
    wire [32:0] invalidated_after = {1'b0,invalidated_total} + {1'b0,invalidating};
    always_comb begin
        invalidating = '0;
        for (integer slot = 0; slot < SLOTS; slot++)
            invalidating = invalidating + 32'(retiring[slot]);
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            cursor <= '0;
            grant_pending <= 1'b0;
            {grant_status,grant_claim,grant_tag,grant_slot,grant_generation,
             grant_instrument,grant_buy,grant_children} <= '0;
            {reserved_total,dispatched_total,released_total,invalidated_total,revoked_grants} <= '0;
        end else begin
            if (book && book_space) begin
                cursor <= book_slot == SLOT_W'(SLOTS-1) ? '0 : INDEX_W'(book_slot) + 1'b1;
                if (!(&reserved_total))
                    reserved_total <= reserved_total + 1'b1;
            end
            invalidated_total <= invalidated_after[32] ? '1 : invalidated_after[31:0];
            if (dispatch && !(&dispatched_total))
                dispatched_total <= dispatched_total + 1'b1;
            if (release_fire && !(&released_total))
                released_total <= released_total + 1'b1;
            if (grant_fire && grant_claim && grant_status == OK && !grant_applicable && !(&revoked_grants))
                revoked_grants <= revoked_grants + 1'b1;

            if (grant_fire)
                grant_pending <= 1'b0;
            if (action_fire) begin
                grant_pending <= 1'b1;
                grant_status <= outcome;
                grant_claim <= action_claim;
                grant_tag <= action_tag;
                grant_slot <= action_slot;
                grant_generation <= action_generation;
                grant_instrument <= outcome == OK ? queried.instrument : '0;
                grant_buy <= outcome == OK && queried.buy;
                grant_children <= outcome == OK && action_claim ? queried.children : '0;
            end
        end
    end

`ifdef FORMAL
    logic initialized = 1'b0;
    always_ff @(posedge clk) begin
        initialized <= 1'b1;
        if (initialized && !rst && !$past(rst)) begin
            if ($past(grant_valid && !grant_ready)) begin
                assert(grant_valid && $stable({grant_status,grant_claim,grant_tag,grant_slot,
                       grant_generation,grant_instrument,grant_buy,grant_children}));
                if (!$past(grant_applicable))
                    assert(!grant_applicable);
            end
            assert(reservation_count <= SLOT_W'(SLOTS));
            if (dispatch) begin
                assert(alive[grant_index] && grant_generation == granted.generation);
                assert(!policy_change);
                if (mutation && mutation_instrument == grant_instrument && int'(mutation_venue) < VENUES)
                    assert(dispatch_amounts[QTY_W*change_column +: QTY_W] == 0);
            end
        end
    end
`endif
endmodule

`default_nettype wire
