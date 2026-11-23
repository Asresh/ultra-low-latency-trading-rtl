`timescale 1ns/1ps

module mold_reorder #(
    parameter integer MESSAGE_BYTES = 64,
    parameter integer WINDOW = 16,
    parameter integer GAP_TICKS = 64,
    parameter integer REQUEST_MAX = 32
) (
    input  logic                        clk,
    input  logic                        clear,
    input  logic                        session_active,
    input  logic [63:0]                 seed_sequence,
    input  logic                        s_valid,
    output logic                        s_ready,
    input  logic [2:0]                  s_kind,
    input  logic [63:0]                 s_sequence,
    input  logic [15:0]                 s_length,
    input  logic [MESSAGE_BYTES*8-1:0] s_payload,

    output logic                        m_valid,
    input  logic                        m_ready,
    output logic [63:0]                 m_sequence,
    output logic [15:0]                 m_length,
    output logic [MESSAGE_BYTES*8-1:0] m_payload,

    output logic                        request_valid,
    input  logic                        request_ready,
    output logic [63:0]                 request_sequence,
    output logic [15:0]                 request_count,

    output logic [63:0]                 expected_sequence,
    output logic [63:0]                 high_water,
    output logic                        gap_pending,
    output logic                        session_end_seen,
    output logic [5:0]                  occupancy,

    output logic                        hit_store,
    output logic                        hit_duplicate,
    output logic                        hit_conflict,
    output logic                        hit_stale,
    output logic                        hit_far
);
    localparam logic [2:0] DATA = 3'd0, HEARTBEAT = 3'd1, END_SESSION = 3'd2;
    localparam integer SLOT_BITS = $clog2(WINDOW);
    localparam integer TIMER_BITS = $clog2(GAP_TICKS+1);
    localparam integer RECORD_BITS = 16 + MESSAGE_BYTES*8;
    localparam logic [63:0] HALF_RANGE = 64'h8000_0000_0000_0000;

    logic [WINDOW-1:0] present;
    logic [RECORD_BITS-1:0] slots [0:WINDOW-1];
    logic [SLOT_BITS-1:0] target_slot, head_slot, scan_slot;
    logic [63:0] relative_sequence, hint_sequence, hinted_distance;
    logic [63:0] hint_from_expected;
    logic [63:0] missing_count;
    logic [15:0] next_request_count;
    logic [TIMER_BITS-1:0] idle_ticks;
    logic accept, release_head, update_hint, found_ready;

    assign s_ready = !clear;
    assign accept = s_valid && s_ready;
    assign target_slot = s_sequence[SLOT_BITS-1:0];
    assign head_slot = expected_sequence[SLOT_BITS-1:0];

    assign relative_sequence = s_sequence - expected_sequence;
    assign hint_sequence = s_kind == DATA ? s_sequence + 64'd1 : s_sequence;
    assign hint_from_expected = hint_sequence - expected_sequence;
    assign hinted_distance = high_water - expected_sequence;
    // Only a forward DATA message can prove the exclusive half-range watermark.
    assign update_hint = accept && session_active &&
                         (s_kind == DATA || s_kind == HEARTBEAT || s_kind == END_SESSION) &&
                         hint_from_expected > hinted_distance &&
                         (!hint_from_expected[63] || (s_kind == DATA && !relative_sequence[63]));

    assign hit_stale = accept && session_active && s_kind == DATA && relative_sequence[63];
    assign hit_far = accept && session_active && s_kind == DATA
                  && !relative_sequence[63] && relative_sequence >= 64'(WINDOW);
    assign hit_duplicate = accept && session_active && s_kind == DATA
                        && relative_sequence < 64'(WINDOW) && present[target_slot];
    assign hit_conflict = hit_duplicate && slots[target_slot] != {s_length, s_payload};
    assign hit_store = accept && session_active && s_kind == DATA
                    && relative_sequence < 64'(WINDOW) && !present[target_slot];

    assign m_valid = !clear && session_active && present[head_slot];
    assign m_sequence = expected_sequence;
    assign {m_length, m_payload} = slots[head_slot];
    assign release_head = m_valid && m_ready;
    assign gap_pending = session_active && !present[head_slot]
                       && hinted_distance != 0 && hinted_distance <= HALF_RANGE;

    always @* begin
        missing_count = hinted_distance;
        found_ready = 1'b0;
        for (integer distance = 1; distance < WINDOW; distance = distance + 1) begin
            scan_slot = head_slot + SLOT_BITS'(distance);
            if (!found_ready && present[scan_slot]) begin
                missing_count = 64'(distance);
                found_ready = 1'b1;
            end
        end
        next_request_count = missing_count > 64'(REQUEST_MAX)
                           ? 16'(REQUEST_MAX) : missing_count[15:0];
    end

    always_ff @(posedge clk) begin
        if (hit_store)
            slots[target_slot] <= {s_length, s_payload};
    end

    always_ff @(posedge clk) begin
        if (clear) begin
            present           <= '0;
            occupancy         <= '0;
            expected_sequence <= seed_sequence;
            high_water        <= seed_sequence;
            session_end_seen  <= 1'b0;
        end else begin
            if (hit_store)
                present[target_slot] <= 1'b1;
            if (release_head) begin
                present[head_slot] <= 1'b0;
                expected_sequence  <= expected_sequence + 64'd1;
            end
            case ({hit_store, release_head})
                2'b10: occupancy <= occupancy + 6'd1;
                2'b01: occupancy <= occupancy - 6'd1;
                default: begin end
            endcase
            if (update_hint)
                high_water <= hint_sequence;
            if (accept && session_active && s_kind == END_SESSION)
                session_end_seen <= 1'b1;
        end
    end

    always_ff @(posedge clk) begin
        if (clear) begin
            request_valid    <= 1'b0;
            request_sequence <= '0;
            request_count    <= '0;
            idle_ticks       <= '0;
        end else if (request_valid) begin
            if (request_ready) begin
                request_valid <= 1'b0;
                idle_ticks    <= '0;
            end
        end else if (!gap_pending || (hit_store && target_slot == head_slot)) begin
            idle_ticks <= '0;
        end else if (idle_ticks == TIMER_BITS'(GAP_TICKS-1)) begin
            request_valid    <= 1'b1;
            request_sequence <= expected_sequence;
            request_count    <= next_request_count;
        end else begin
            idle_ticks <= idle_ticks + 1'b1;
        end
    end
endmodule
