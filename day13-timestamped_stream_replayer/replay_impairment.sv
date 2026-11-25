`timescale 1ns/1ps
`default_nettype none

// A packet-aware replay stage. The source and sink use the same saturating
// wall clock as the banked replay manager.
module replay_impairment #(
    parameter integer RULES = 4,
    parameter integer QUEUE_DEPTH = 4,
    parameter integer MAX_BEATS = 16,
    parameter integer RUN_W = 32,
    parameter integer REV_W = 16,
    localparam integer KEY_W = $clog2(RULES+1),
    localparam integer RULE_W = 198+RUN_W,
    localparam integer AUDIT_W = 280+RUN_W+REV_W+KEY_W,
    localparam integer OCC_W = $clog2(QUEUE_DEPTH+1)
) (
    input wire clk, rst,
    input wire [63:0] wall_tick,

    input wire cfg_valid,
    output wire cfg_ready,
    input wire [1:0] cfg_op,
    input wire [REV_W-1:0] cfg_expected_revision,
    input wire [KEY_W-1:0] cfg_slot,
    input wire cfg_read_active,
    input wire [31:0] cfg_seed,
    input wire [RULE_W-1:0] cfg_rule,
    output wire rsp_valid,
    input wire rsp_ready,
    output wire [1:0] rsp_op,
    output wire [2:0] rsp_status,
    output wire [REV_W-1:0] rsp_revision,
    output wire [KEY_W-1:0] rsp_slot,
    output wire [31:0] rsp_seed,
    output wire [RULE_W-1:0] rsp_rule,

    input wire s_valid,
    output wire s_ready,
    input wire s_first, s_last,
    input wire [63:0] s_data,
    input wire [7:0] s_keep,
    input wire [15:0] s_tag, s_pass,
    input wire [47:0] s_due, s_offered,
    input wire s_bank,
    input wire [RUN_W-1:0] s_run,
    input wire abort_valid, abort_bank,
    input wire [RUN_W-1:0] abort_run,

    output wire m_valid,
    input wire m_ready,
    output wire m_first, m_last, m_copy,
    output wire [63:0] m_data,
    output wire [7:0] m_keep,
    output wire [15:0] m_tag, m_pass,
    output wire [47:0] m_due, m_offered,
    output wire m_bank,
    output wire [RUN_W-1:0] m_run,
    output wire [REV_W-1:0] m_revision,
    output wire [31:0] m_ordinal,
    output wire [1:0] m_action,
    output wire [63:0] m_eligible,

    output wire audit_valid,
    input wire audit_ready,
    output logic [63:0] audit_sequence, audit_cursor,
    output logic [AUDIT_W-1:0] audit_data,
    output logic [OCC_W-1:0] occupancy, peak_occupancy,
    output logic [31:0] frames, drops, duplicates, corruptions, flip_misses,
    output logic [31:0] malformed, oversizes, aborted, emitted, lost_audits,
    output logic [31:0] sink_stall_cycles
);
    localparam logic [1:0] PASS = 0, DROP = 1, DUPLICATE = 2, CORRUPT = 3;
    localparam logic [3:0] FRAMING = 4, CAPACITY = 5, TRUNCATED = 6, FLIP_MISS = 7;
    localparam integer SLOT_W = QUEUE_DEPTH > 1 ? $clog2(QUEUE_DEPTH) : 1;
    localparam integer POS_W = MAX_BEATS > 1 ? $clog2(MAX_BEATS) : 1;
    localparam integer LEN_W = $clog2(MAX_BEATS+2);
    localparam integer BEAT_W = 168;

    typedef struct packed {
        logic bank;
        logic [RUN_W-1:0] run_id;
        logic [15:0] tag, pass_id;
        logic [31:0] ordinal;
        logic [REV_W-1:0] revision;
        logic [KEY_W-1:0] rule_slot;
        logic [1:0] action;
        logic [63:0] arrival, eligible;
        logic [31:0] copy_gap;
        logic [15:0] flip_offset;
        logic [7:0] flip_mask;
    } packet_t;

    typedef struct packed {
        packet_t packet;
        logic [LEN_W-1:0] length;
    } queued_t;

    logic [BEAT_W-1:0] payload [0:QUEUE_DEPTH-1][0:MAX_BEATS-1];
    queued_t metadata [0:QUEUE_DEPTH-1];
    logic [SLOT_W-1:0] read_slot, write_slot;
    logic open_packet, known_run, source_bank;
    logic [RUN_W-1:0] source_run;
    logic [31:0] next_ordinal;
    logic [3:0] saved_fault;
    logic [16:0] saved_bytes;
    logic [LEN_W-1:0] saved_beats;
    packet_t capturing;
    logic emitting, second_copy, audit_pending;
    logic [POS_W-1:0] output_index;
    logic [63:0] copy_release;

    wire [31:0] ordinal = known_run && s_run == source_run && s_bank == source_bank ?
                          next_ordinal : 32'd0;
    wire [KEY_W-1:0] rule_slot;
    wire [REV_W-1:0] revision;
    wire [1:0] policy_action;
    wire [32:0] policy_delay;
    wire [31:0] copy_gap;
    wire [15:0] flip_offset;
    wire [7:0] flip_mask;

    replay_impairment_rules #(.RULES(RULES), .RUN_W(RUN_W), .REV_W(REV_W)) rules (
        .clk, .rst, .cfg_valid, .cfg_ready, .cfg_op, .cfg_expected_revision, .cfg_slot,
        .cfg_read_active, .cfg_seed, .cfg_rule, .rsp_valid, .rsp_ready, .rsp_op, .rsp_status,
        .rsp_revision, .rsp_slot, .rsp_seed, .rsp_rule, .resolve_bank(s_bank),
        .resolve_run(s_run), .resolve_tag(s_tag), .resolve_pass(s_pass),
        .resolve_ordinal(ordinal), .resolved_slot(rule_slot), .resolved_revision(revision),
        .resolved_action(policy_action), .resolved_delay(policy_delay),
        .resolved_copy_gap(copy_gap), .resolved_flip_offset(flip_offset), .resolved_flip_mask(flip_mask)
    );

    function automatic logic [31:0] inc32(input logic [31:0] value);
        inc32 = (&value) ? value : value + 1'b1;
    endfunction

    function automatic logic [63:0] sat_tick(input logic [63:0] tick, delay_value);
        logic [64:0] total;
        begin
            total = {1'b0,tick} + {1'b0,delay_value};
            sat_tick = total[64] ? '1 : total[63:0];
        end
    endfunction

    function automatic logic [SLOT_W-1:0] advance(input logic [SLOT_W-1:0] value);
        advance = value == SLOT_W'(QUEUE_DEPTH-1) ? '0 : value + 1'b1;
    endfunction

    function automatic logic [3:0] bytes_for_keep(input logic [7:0] keep);
        case (keep)
            8'h01: bytes_for_keep = 1;
            8'h03: bytes_for_keep = 2;
            8'h07: bytes_for_keep = 3;
            8'h0f: bytes_for_keep = 4;
            8'h1f: bytes_for_keep = 5;
            8'h3f: bytes_for_keep = 6;
            8'h7f: bytes_for_keep = 7;
            8'hff: bytes_for_keep = 8;
            default: bytes_for_keep = 0;
        endcase
    endfunction

    wire packet_t first_desc = {
        s_bank,s_run,s_tag,s_pass,ordinal,revision,rule_slot,policy_action,wall_tick,
        sat_tick(wall_tick,64'(policy_delay)),copy_gap,flip_offset,flip_mask
    };
    wire packet_t desc = open_packet ? capturing : first_desc;
    wire queued_t head = metadata[read_slot];
    wire abort_capture = abort_valid && open_packet &&
                         abort_bank == capturing.bank && abort_run == capturing.run_id;
    wire [63:0] selected_release = second_copy ? copy_release : head.packet.eligible;
    wire release_now = occupancy != 0 && wall_tick >= selected_release;
    wire transfer_in = s_valid && s_ready;
    wire transfer_out = m_valid && m_ready;
    wire finish_copy = transfer_out && m_last;
    wire duplicate_head = head.packet.action == DUPLICATE;
    wire pop = finish_copy && (!duplicate_head || second_copy);
    wire [LEN_W-1:0] prior_beats = open_packet ? saved_beats : '0;
    wire [LEN_W-1:0] after_beats = prior_beats == LEN_W'(MAX_BEATS+1) ?
                                  prior_beats : prior_beats + 1'b1;
    wire [16:0] prior_bytes = open_packet ? saved_bytes : '0;
    wire [3:0] byte_count = bytes_for_keep(s_keep);
    wire [17:0] extended_bytes = {1'b0,prior_bytes} + 18'(byte_count);
    wire [16:0] after_bytes = extended_bytes[17] ? '1 : extended_bytes[16:0];

    wire framing_bad = s_first == open_packet || byte_count == 0 ||
                       (!s_last && s_keep != 8'hff) ||
                       (open_packet && (s_bank != desc.bank || s_run != desc.run_id ||
                                        s_tag != desc.tag || s_pass != desc.pass_id));
    wire [3:0] fault_now = open_packet && saved_fault != 0 ? saved_fault :
                           framing_bad ? FRAMING : after_beats > LEN_W'(MAX_BEATS) ? CAPACITY : 4'd0;
    wire physical_end = transfer_in && s_last;
    wire complete_capture = physical_end || abort_capture;
    wire missed_flip = desc.action == CORRUPT && {1'b0,desc.flip_offset} >= after_bytes;
    wire [3:0] verdict = abort_capture ? TRUNCATED :
                         fault_now != 0 ? fault_now : missed_flip ? FLIP_MISS : 4'(desc.action);
    wire [1:0] effective_action = missed_flip ? PASS : desc.action;
    wire packet_t queue_packet = {
        desc.bank,desc.run_id,desc.tag,desc.pass_id,desc.ordinal,desc.revision,desc.rule_slot,
        effective_action,desc.arrival,desc.eligible,desc.copy_gap,desc.flip_offset,desc.flip_mask
    };
    wire push = physical_end && fault_now == 0 && desc.action != DROP;
    wire [OCC_W-1:0] occupancy_next = push && !pop ? occupancy + 1'b1 :
                                      pop && !push ? occupancy - 1'b1 : occupancy;
    wire [63:0] audit_tick = wall_tick;
    wire [16:0] audit_bytes = abort_capture ? prior_bytes : after_bytes;
    wire audit_terminal = &audit_cursor;
    wire can_audit = !audit_terminal && (!audit_pending || audit_ready);
    wire [BEAT_W-1:0] raw_word = payload[read_slot][output_index];
    wire [63:0] raw_data;
    wire [7:0] raw_keep;
    wire [47:0] raw_due, raw_offered;
    assign {raw_data,raw_keep,raw_due,raw_offered} = raw_word;

    assign s_ready = !rst && !abort_capture &&
                     (open_packet || occupancy < OCC_W'(QUEUE_DEPTH) || pop || policy_action == DROP);
    assign m_valid = !rst && emitting;
    assign m_first = m_valid && output_index == 0;
    assign m_last = m_valid && LEN_W'(output_index) + 1'b1 == head.length;
    assign m_copy = m_valid && second_copy;
    assign m_keep = m_valid ? raw_keep : '0;
    assign m_tag = m_valid ? head.packet.tag : '0;
    assign m_pass = m_valid ? head.packet.pass_id : '0;
    assign m_due = m_valid ? raw_due : '0;
    assign m_offered = m_valid ? raw_offered : '0;
    assign m_bank = m_valid && head.packet.bank;
    assign m_run = m_valid ? head.packet.run_id : '0;
    assign m_revision = m_valid ? head.packet.revision : '0;
    assign m_ordinal = m_valid ? head.packet.ordinal : '0;
    assign m_action = m_valid ? head.packet.action : PASS;
    assign m_eligible = m_valid ? selected_release : '0;
    assign audit_valid = !rst && audit_pending;

    for (genvar lane = 0; lane < 8; lane++) begin : output_bytes
        wire [31:0] byte_offset = (32'(output_index) << 3) + 32'(lane);
        wire flip = head.packet.action == CORRUPT && byte_offset == 32'(head.packet.flip_offset);
        assign m_data[lane*8+:8] = m_keep[lane] ?
            raw_data[lane*8+:8] ^ (flip ? head.packet.flip_mask : 8'd0) : 8'd0;
    end

    always_ff @(posedge clk) begin
        if (!rst && transfer_in && fault_now == 0 && desc.action != DROP)
            payload[write_slot][POS_W'(prior_beats)] <= {s_data,s_keep,s_due,s_offered};
        if (!rst && push)
            metadata[write_slot] <= {queue_packet,after_beats};
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            open_packet <= 0;
            known_run <= 0;
            source_bank <= 0;
            source_run <= '0;
            next_ordinal <= '0;
            saved_beats <= '0;
            saved_bytes <= '0;
            saved_fault <= '0;
            capturing <= '0;
            write_slot <= '0;
            read_slot <= '0;
            emitting <= 0;
            output_index <= '0;
            second_copy <= 0;
            copy_release <= '0;
            occupancy <= '0;
            peak_occupancy <= '0;
        end else begin
            if (abort_capture) begin
                open_packet <= 0;
            end else if (transfer_in) begin
                open_packet <= !s_last;
                if (!open_packet) begin
                    known_run <= 1;
                    source_bank <= s_bank;
                    source_run <= s_run;
                    next_ordinal <= inc32(ordinal);
                    capturing <= first_desc;
                end
                saved_beats <= after_beats;
                saved_bytes <= after_bytes;
                saved_fault <= fault_now;
            end
            if (push)
                write_slot <= advance(write_slot);
            if (pop)
                read_slot <= advance(read_slot);
            occupancy <= occupancy_next;
            if (occupancy_next > peak_occupancy)
                peak_occupancy <= occupancy_next;

            if (!emitting && release_now)
                emitting <= 1;
            if (finish_copy) begin
                emitting <= 0;
                output_index <= '0;
                second_copy <= duplicate_head && !second_copy;
                if (duplicate_head && !second_copy)
                    copy_release <= sat_tick(wall_tick,64'(head.packet.copy_gap) + 1'b1);
            end else if (transfer_out) begin
                output_index <= output_index + 1'b1;
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            audit_pending <= 0;
            audit_sequence <= '0;
            audit_cursor <= '0;
            audit_data <= '0;
            frames <= '0;
            drops <= '0;
            duplicates <= '0;
            corruptions <= '0;
            flip_misses <= '0;
            malformed <= '0;
            oversizes <= '0;
            aborted <= '0;
            emitted <= '0;
            lost_audits <= '0;
            sink_stall_cycles <= '0;
        end else begin
            if (audit_ready)
                audit_pending <= 0;
            if (complete_capture) begin
                frames <= inc32(frames);
                if (!audit_terminal)
                    audit_cursor <= audit_cursor + 1'b1;
                if (can_audit) begin
                    audit_pending <= 1;
                    audit_sequence <= audit_cursor;
                    audit_data <= {
                        desc.bank,desc.run_id,desc.tag,desc.pass_id,desc.ordinal,desc.revision,
                        desc.rule_slot,desc.action,verdict,desc.arrival,audit_tick,desc.eligible,audit_bytes
                    };
                end else begin
                    lost_audits <= inc32(lost_audits);
                end
                case (verdict)
                    4'(DROP): drops <= inc32(drops);
                    4'(DUPLICATE): duplicates <= inc32(duplicates);
                    4'(CORRUPT): corruptions <= inc32(corruptions);
                    FRAMING: malformed <= inc32(malformed);
                    CAPACITY: oversizes <= inc32(oversizes);
                    TRUNCATED: aborted <= inc32(aborted);
                    FLIP_MISS: flip_misses <= inc32(flip_misses);
                    default: begin end
                endcase
            end
            if (finish_copy)
                emitted <= inc32(emitted);
            if (m_valid && !m_ready)
                sink_stall_cycles <= inc32(sink_stall_cycles);
        end
    end
endmodule

`default_nettype wire
