`timescale 1ns/1ps
`default_nettype none

// Retain old output through a cutover, verify candidate deltas independently,
// and drain an approved delta interval before allowing new direct target events.
module mold_snapshot_router #(
    parameter integer SLOTS = 3,
    parameter integer EPOCH_W = 16,
    parameter integer DELTAS = 8,
    localparam integer KEY_W = $clog2(SLOTS+1),
    localparam integer OCC_W = $clog2(DELTAS+1)
) (
    input wire clk, rst,
    input wire scope_start, scope_fenced, scope_accept,
    input wire scope_finish, scope_commit,
    input wire [KEY_W-1:0] scope_slot,
    input wire [EPOCH_W-1:0] scope_epoch,
    input wire [63:0] scope_sequence,
    input wire native_observed,
    input wire [EPOCH_W-1:0] observed_epoch,
    output wire old_output_held,
    output logic scope_gap,
    output wire gap_at_cut,
    output logic [63:0] candidate_sequence,
    output logic [OCC_W-1:0] delta_used,
    output wire [OCC_W-1:0] delta_at_cut,
    output logic draining,

    input wire s_valid,
    output wire s_ready,
    input wire [KEY_W-1:0] s_slot,
    input wire [EPOCH_W-1:0] s_epoch,
    input wire [587:0] s_data,
    output wire m_valid,
    input wire m_ready,
    output logic [KEY_W-1:0] m_slot,
    output logic [EPOCH_W-1:0] m_epoch,
    output logic [587:0] m_data,

    input wire [KEY_W-1:0] monitor_slot,
    output wire monitor_valid, monitor_known, monitor_quarantined,
    output wire [EPOCH_W-1:0] monitor_epoch,
    output wire [63:0] monitor_sequence,
    output wire [31:0] monitor_stale, monitor_gap, monitor_delivered, monitor_buffered,
    output logic [31:0] invalid_slots
);
    localparam integer INDEX_W = SLOTS > 1 ? $clog2(SLOTS) : 1;
    localparam integer PTR_W = DELTAS > 1 ? $clog2(DELTAS) : 1;
    localparam logic [2:0] DIRECT = 0, BUFFER = 1, STALE = 2, GAP = 3, BLOCK = 4, INVALID = 5;
    logic [SLOTS-1:0] known, quarantined;
    logic [EPOCH_W-1:0] epochs [0:SLOTS-1];
    logic [63:0] sequences [0:SLOTS-1];
    logic [31:0] stale_counts [0:SLOTS-1], gap_counts [0:SLOTS-1];
    logic [31:0] delivered_counts [0:SLOTS-1], buffered_counts [0:SLOTS-1];
    logic [587:0] queue [0:DELTAS-1];
    logic [PTR_W-1:0] read_index, write_index;
    logic pending, favor_delta;
    logic [2:0] disposition;

    wire key_ok = int'(s_slot) < SLOTS;
    wire [INDEX_W-1:0] input_index = key_ok ? INDEX_W'(s_slot) : '0;
    wire selected_scope = s_slot == scope_slot;
    wire [63:0] input_sequence = s_data[583:520];
    wire space = !pending || m_ready;
    wire want_delta = draining && delta_used != 0;
    wire take_delta = !rst && space && want_delta &&
                      (favor_delta || !s_valid || disposition != DIRECT);
    wire take_input = s_valid && s_ready;
    wire forward = take_input && disposition == DIRECT;
    wire push = take_input && disposition == BUFFER;
    wire bad_delta = take_input && disposition == GAP && selected_scope && scope_fenced;
    wire [63:0] cursor_at_cut = candidate_sequence + 64'(push);
    wire [OCC_W-1:0] used_next = push ? delta_used + 1'b1 :
                                    take_delta ? delta_used - 1'b1 : delta_used;

    assign m_valid = !rst && pending;
    assign old_output_held = pending && m_slot == scope_slot;
    assign gap_at_cut = scope_gap || bad_delta;
    assign delta_at_cut = used_next;
    assign s_ready = !rst && (disposition == DIRECT ? space && !take_delta :
                             disposition == BUFFER ? delta_used < OCC_W'(DELTAS) :
                             disposition != BLOCK);

    wire monitor_key_ok = int'(monitor_slot) < SLOTS;
    wire [INDEX_W-1:0] monitor_index = monitor_key_ok ? INDEX_W'(monitor_slot) : '0;
    assign monitor_valid = !rst && monitor_key_ok;
    assign monitor_known = monitor_valid && known[monitor_index];
    assign monitor_quarantined = monitor_valid && quarantined[monitor_index];
    assign monitor_epoch = monitor_known ? epochs[monitor_index] : '0;
    assign monitor_sequence = monitor_known ? sequences[monitor_index] : '0;
    assign monitor_stale = monitor_valid ? stale_counts[monitor_index] : '0;
    assign monitor_gap = monitor_valid ? gap_counts[monitor_index] : '0;
    assign monitor_delivered = monitor_valid ? delivered_counts[monitor_index] : '0;
    assign monitor_buffered = monitor_valid ? buffered_counts[monitor_index] : '0;

    always @* begin
        disposition = DIRECT;
        if (!key_ok)
            disposition = INVALID;
        else if (selected_scope && scope_fenced) begin
            if (!scope_accept || s_epoch != scope_epoch)
                disposition = STALE;
            else if (scope_gap || input_sequence != candidate_sequence)
                disposition = GAP;
            else
                disposition = BUFFER;
        end else if (selected_scope && draining)
            disposition = BLOCK;
        else if (quarantined[input_index] || (known[input_index] && s_epoch != epochs[input_index]))
            disposition = STALE;
        else if (known[input_index] && input_sequence != sequences[input_index])
            disposition = GAP;
    end

    function automatic logic [PTR_W-1:0] next_ptr(input logic [PTR_W-1:0] ptr);
        next_ptr = ptr == PTR_W'(DELTAS-1) ? '0 : ptr + 1'b1;
    endfunction

    function automatic logic [31:0] increment(input logic [31:0] count);
        increment = (&count) ? count : count + 1'b1;
    endfunction

    always_ff @(posedge clk) begin
        if (push)
            queue[write_index] <= s_data;
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            delta_used <= 0;
            read_index <= 0;
            write_index <= 0;
            candidate_sequence <= 0;
            scope_gap <= 0;
            draining <= 0;
        end else if (scope_start) begin
            delta_used <= 0;
            read_index <= 0;
            write_index <= 0;
            candidate_sequence <= scope_sequence;
            scope_gap <= 0;
            draining <= 0;
        end else begin
            if (push) begin
                write_index <= next_ptr(write_index);
                candidate_sequence <= cursor_at_cut;
            end
            if (take_delta)
                read_index <= next_ptr(read_index);
            if (bad_delta)
                scope_gap <= 1;
            if (scope_finish)
                draining <= scope_commit && used_next != 0;
            else if (draining && used_next == 0)
                draining <= 0;
            delta_used <= scope_finish && !scope_commit ? '0 : used_next;
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            pending <= 0;
            favor_delta <= 0;
            m_slot <= 0;
            m_epoch <= 0;
            m_data <= 0;
            invalid_slots <= 0;
        end else begin
            if (space)
                pending <= take_delta || forward;
            if (take_delta) begin
                favor_delta <= 0;
                m_slot <= scope_slot;
                m_epoch <= scope_epoch;
                m_data <= queue[read_index];
            end else if (forward) begin
                favor_delta <= 1;
                m_slot <= s_slot;
                m_epoch <= s_epoch;
                m_data <= s_data;
            end
            if (take_input && disposition == INVALID)
                invalid_slots <= increment(invalid_slots);
        end
    end

    for (genvar slot = 0; slot < SLOTS; slot++) begin : monitored_slot
        localparam logic [KEY_W-1:0] SLOT = KEY_W'(slot);
        wire scoped = scope_slot == SLOT;
        wire own_input = take_input && s_slot == SLOT;
        wire stream_gap = own_input && disposition == GAP && !(scope_fenced && scoped);

        always_ff @(posedge clk) begin
            if (rst) begin
                known[slot] <= 0;
                quarantined[slot] <= 0;
                epochs[slot] <= 0;
                sequences[slot] <= 0;
                stale_counts[slot] <= 0;
                gap_counts[slot] <= 0;
                delivered_counts[slot] <= 0;
                buffered_counts[slot] <= 0;
            end else begin
                if (native_observed && scoped) begin
                    known[slot] <= 1;
                    epochs[slot] <= observed_epoch;
                end
                if (scope_finish && scoped) begin
                    quarantined[slot] <= !scope_commit;
                    if (scope_commit)
                        sequences[slot] <= cursor_at_cut;
                end else if (stream_gap) begin
                    quarantined[slot] <= 1;
                end else if (own_input && forward && known[slot]) begin
                    sequences[slot] <= sequences[slot] + 1'b1;
                end
                if (own_input && disposition == STALE)
                    stale_counts[slot] <= increment(stale_counts[slot]);
                if (own_input && disposition == GAP)
                    gap_counts[slot] <= increment(gap_counts[slot]);
                if (own_input && push)
                    buffered_counts[slot] <= increment(buffered_counts[slot]);
                if (m_valid && m_ready && m_slot == SLOT)
                    delivered_counts[slot] <= increment(delivered_counts[slot]);
            end
        end
    end
endmodule

`default_nettype wire
