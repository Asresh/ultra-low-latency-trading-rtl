`timescale 1ns/1ps
`default_nettype none
`include "reconcile_defs.svh"

// Reconcile one timestamped execution from each of two independent observations.
module drop_copy_reconciliation #(
    parameter integer SETS = 4,
    parameter integer WAIT_CYCLES = 24,
    parameter integer HISTORY_CYCLES = 40,
    parameter logic [31:0] RESET_TICK = 0,
    localparam integer SLOTS = 2 * SETS,
    localparam integer SIDX_W = $clog2(SLOTS),
    localparam integer SET_W = (SETS == 1) ? 1 : $clog2(SETS),
    localparam integer COUNT_W = $clog2(SLOTS+1)
) (
    input  wire                         clk,
    input  wire                         rst,
    input  wire                         s_primary_valid,
    output wire                         s_primary_ready,
    input  wire [`DC_KEY_W-1:0]         s_primary_key,
    input  wire [`DC_PAYLOAD_W-1:0]     s_primary_payload,
    input  wire                         s_copy_valid,
    output wire                         s_copy_ready,
    input  wire [`DC_KEY_W-1:0]         s_copy_key,
    input  wire [`DC_PAYLOAD_W-1:0]     s_copy_payload,
    input  wire                         s_epoch_valid,
    output wire                         s_epoch_ready,
    input  wire [15:0]                  s_new_epoch,
    output logic                        m_valid,
    input  wire                         m_ready,
    output logic [3:0]                  m_status,
    output logic [1:0]                  m_source,
    output logic [`DC_KEY_W-1:0]        m_key,
    output logic [`DC_PAYLOAD_W-1:0]    m_expected,
    output logic [`DC_PAYLOAD_W-1:0]    m_observed,
    output logic [3:0]                  m_diff,
    output logic [31:0]                 m_age,
    output logic [COUNT_W-1:0]          m_flushed,
    output logic [15:0]                 active_epoch,
    output logic [COUNT_W-1:0]          pending_count,
    output logic [31:0]                 tick,
    output logic [31:0]                 observed_count,
    output logic [31:0]                 matched_count,
    output logic [31:0]                 conflict_count,
    output logic [31:0]                 duplicate_count,
    output logic [31:0]                 missing_count,
    output logic [31:0]                 late_count,
    output logic [31:0]                 full_count,
    output logic [31:0]                 rejected_count,
    output logic [31:0]                 flushed_count
);

    if (SETS < 1 || (SETS & (SETS-1)) != 0) begin : invalid_sets
        initial $fatal(1, "SETS must be a positive power of two");
    end
    if (WAIT_CYCLES < 1 || HISTORY_CYCLES < 1) begin : invalid_duration
        initial $fatal(1, "WAIT_CYCLES and HISTORY_CYCLES must be positive");
    end

    localparam logic [1:0] EMPTY = 0, WAITING = 1, PAIRED = 2, TIMED_OUT = 3;
    logic [1:0] state [0:SLOTS-1];
    logic [`DC_KEY_W-1:0] keys [0:SLOTS-1];
    logic [`DC_PAYLOAD_W-1:0] first_payload [0:SLOTS-1];
    logic [`DC_PAYLOAD_W-1:0] second_payload [0:SLOTS-1];
    logic [31:0] born [0:SLOTS-1];
    logic first_was_copy [0:SLOTS-1];
    logic prefer_copy;
    logic [SIDX_W-1:0] sweep;

    // Fold every normalized key bit into its bucket. Full keys still decide equality.
    function automatic logic [SET_W-1:0] bucket(input logic [`DC_KEY_W-1:0] key);
        logic [SET_W-1:0] hash;
        begin
            hash = 0;
            for (integer bit_index = 0; bit_index < `DC_KEY_W; bit_index++)
                hash[bit_index % SET_W] = hash[bit_index % SET_W] ^ key[bit_index];
            bucket = (SETS == 1) ? '0 : hash;
        end
    endfunction

    // Compare fields independently so the result also explains a multi-field mismatch.
    function automatic logic [3:0] different(
        input logic [`DC_PAYLOAD_W-1:0] a,
        input logic [`DC_PAYLOAD_W-1:0] b
    );
        begin
            different = {
                a[88:57] != b[88:57],  // order reference
                a[56:33] != b[56:33],  // fill quantity
                a[32:1] != b[32:1],    // execution price
                a[0] != b[0]          // side
            };
        end
    endfunction

    // Prevent the public counters from wrapping when diagnostics run continuously.
    function automatic logic [31:0] add_sat(input logic [31:0] n, delta);
        logic [32:0] sum;
        begin
            sum = {1'b0,n} + {1'b0,delta};
            add_sat = sum[32] ? '1 : sum[31:0];
        end
    endfunction

    wire room = !rst && (!m_valid || m_ready);
    wire config_fire = s_epoch_valid && s_epoch_ready;
    wire use_copy = s_copy_valid && (!s_primary_valid || prefer_copy);
    wire [`DC_KEY_W-1:0] key = use_copy ? s_copy_key : s_primary_key;
    wire [`DC_PAYLOAD_W-1:0] payload =
        use_copy ? s_copy_payload : s_primary_payload;
    wire correct_epoch = key[73:58] == active_epoch;
    wire valid_event = key[33:2] != 0 && key[1:0] <= 2'd1 && payload[56:33] != 0;
    wire [SIDX_W-1:0] base;
    if (SETS == 1) begin : one_set
        assign base = '0;
    end else begin : multiple_sets
        assign base = {bucket(key), 1'b0};
    end
    wire [31:0] ages [0:SLOTS-1];
    wire [SLOTS-1:0] due, retired;
    for (genvar slot = 0; slot < SLOTS; slot++) begin : record_age
        assign ages[slot] = tick - born[slot];
        assign due[slot] = state[slot] == WAITING && ages[slot] >= 32'(WAIT_CYCLES);
        assign retired[slot] = (state[slot] == PAIRED || state[slot] == TIMED_OUT) &&
                               ages[slot] >= 32'(HISTORY_CYCLES);
    end
    logic expired_available;
    logic [SIDX_W-1:0] expired;
    logic hit, vacant;
    logic [SIDX_W-1:0] hit_at, vacancy;

    // Rotate diagnostic priority after each expiry when several events are due.
    always @* begin : choose_expiry
        logic [SIDX_W-1:0] scan_index;
        expired_available = 0;
        expired = sweep;
        for (integer offset = 0; offset < SLOTS; offset++) begin
            scan_index = sweep + SIDX_W'(offset);
            if (!expired_available && due[scan_index]) begin
                expired_available = 1;
                expired = scan_index;
            end
        end
    end

    // Pick the first matching way, or the first unused or expired terminal way.
    always @* begin : choose_way
        logic [SIDX_W-1:0] at;
        logic live;
        hit = 0;
        vacant = 0;
        hit_at = base;
        vacancy = base;
        for (integer way = 0; way < 2; way++) begin
            at = base + SIDX_W'(way);
            live = state[at] != EMPTY && !retired[at];
            if (!hit && live && keys[at] == key) begin
                hit = 1;
                hit_at = at;
            end
            if (!vacant && !live) begin
                vacant = 1;
                vacancy = at;
            end
        end
    end

    wire expire_fire = room && !s_epoch_valid && expired_available;
    wire input_room = room && !s_epoch_valid && !expired_available;
    wire primary_fire = s_primary_valid && s_primary_ready;
    wire copy_fire = s_copy_valid && s_copy_ready;
    wire seen = primary_fire || copy_fire;
    wire same_lane = first_was_copy[hit_at] == use_copy;
    logic [3:0] result, mismatch;
    logic [`DC_PAYLOAD_W-1:0] expected;

    assign s_epoch_ready = room;
    assign s_primary_ready = input_room && !use_copy;
    assign s_copy_ready = input_room && use_copy;

    always @* begin
        result = `DC_STAGED;
        mismatch = 0;
        expected = 0;
        if (!correct_epoch) begin
            result = `DC_STALE_EPOCH;
        end else if (!valid_event) begin
            result = `DC_BAD_EVENT;
        end else if (hit) begin
            if (state[hit_at] == PAIRED && !same_lane)
                expected = second_payload[hit_at];
            else
                expected = first_payload[hit_at];
            mismatch = different(expected, payload);
            if (state[hit_at] == TIMED_OUT && !same_lane)
                result = `DC_LATE;
            else if (state[hit_at] == WAITING && !same_lane)
                result = (mismatch == 0) ? `DC_MATCH : `DC_PAIR_CONFLICT;
            else
                result = (mismatch == 0) ? `DC_DUPLICATE : `DC_DUP_CONFLICT;
        end else if (!vacant) begin
            result = `DC_FULL;
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            tick <= RESET_TICK;
            active_epoch <= 0;
            pending_count <= 0;
            prefer_copy <= 0;
            sweep <= 0;
            m_valid <= 0;
            m_status <= 0;
            m_source <= 0;
            m_key <= 0;
            m_expected <= 0;
            m_observed <= 0;
            m_diff <= 0;
            m_age <= 0;
            m_flushed <= 0;
            observed_count <= 0;
            matched_count <= 0;
            conflict_count <= 0;
            duplicate_count <= 0;
            missing_count <= 0;
            late_count <= 0;
            full_count <= 0;
            rejected_count <= 0;
            flushed_count <= 0;
            for (integer at = 0; at < SLOTS; at++)
                state[at] <= EMPTY;
        end else begin
            tick <= tick + 1'b1;
            // Clear old terminal records even when idle or stalled. A recycled slot below
            // takes precedence, and a future tick wrap cannot resurrect old history.
            for (integer at = 0; at < SLOTS; at++)
                if (retired[at]) state[at] <= EMPTY;
            if (m_valid && m_ready)
                m_valid <= 0;
            if (config_fire) begin
                m_valid <= 1;
                m_source <= 3;
                m_key <= {s_new_epoch,58'd0};
                m_expected <= 0;
                m_observed <= 0;
                m_diff <= 0;
                m_age <= 0;
                m_flushed <= 0;
                if (s_new_epoch > active_epoch) begin
                    m_status <= `DC_RESTART;
                    m_flushed <= pending_count;
                    flushed_count <= add_sat(flushed_count,32'(pending_count));
                    active_epoch <= s_new_epoch;
                    pending_count <= 0;
                    sweep <= 0;
                    prefer_copy <= 0;
                    for (integer at = 0; at < SLOTS; at++)
                        state[at] <= EMPTY;
                end else begin
                    m_status <= `DC_BAD_RESTART;
                    rejected_count <= add_sat(rejected_count,32'd1);
                end
            end else if (expire_fire) begin
                m_valid <= 1;
                m_source <= 2;
                m_status <= first_was_copy[expired] ? `DC_MISSING_PRIMARY : `DC_MISSING_COPY;
                m_key <= keys[expired];
                m_expected <= first_payload[expired];
                m_observed <= 0;
                m_diff <= 0;
                m_age <= ages[expired];
                m_flushed <= 0;
                state[expired] <= TIMED_OUT;
                born[expired] <= tick;
                pending_count <= pending_count - 1'b1;
                sweep <= expired + 1'b1;
                missing_count <= add_sat(missing_count,32'd1);
            end else if (seen) begin
                m_valid <= 1;
                m_source <= {1'b0,use_copy};
                m_status <= result;
                m_key <= key;
                m_expected <= expected;
                m_observed <= payload;
                m_diff <= mismatch;
                m_age <= (hit && correct_epoch && valid_event) ? ages[hit_at] : 32'd0;
                m_flushed <= 0;
                prefer_copy <= !use_copy;
                observed_count <= add_sat(observed_count,32'd1);
                case (result)
                    `DC_STAGED: begin
                        state[vacancy] <= WAITING;
                        keys[vacancy] <= key;
                        first_payload[vacancy] <= payload;
                        first_was_copy[vacancy] <= use_copy;
                        born[vacancy] <= tick;
                        pending_count <= pending_count + 1'b1;
                    end
                    `DC_MATCH,`DC_PAIR_CONFLICT: begin
                        state[hit_at] <= PAIRED;
                        second_payload[hit_at] <= payload;
                        born[hit_at] <= tick;
                        pending_count <= pending_count - 1'b1;
                        if (result == `DC_MATCH)
                            matched_count <= add_sat(matched_count,32'd1);
                        else
                            conflict_count <= add_sat(conflict_count,32'd1);
                    end
                    `DC_DUPLICATE: duplicate_count <= add_sat(duplicate_count,32'd1);
                    `DC_DUP_CONFLICT: conflict_count <= add_sat(conflict_count,32'd1);
                    `DC_LATE: late_count <= add_sat(late_count,32'd1);
                    `DC_FULL: full_count <= add_sat(full_count,32'd1);
                    default: rejected_count <= add_sat(rejected_count,32'd1);
                endcase
            end
        end
    end
endmodule

`default_nettype wire
