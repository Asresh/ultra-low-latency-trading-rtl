`timescale 1ns/1ps
`default_nettype none
`include "mold_recovery_codes.svh"

// Each installed receiver epoch owns at most one outstanding request lease.
module mold_recovery_leases #(
    parameter integer SLOTS = 3,
    parameter integer WINDOW = 16,
    parameter integer EPOCH_W = 16,
    parameter integer TOKEN_W = 16,
    localparam integer KEY_W = $clog2(SLOTS+1),
    localparam integer SLOT_W = SLOTS > 1 ? $clog2(SLOTS) : 1
) (
    input wire clk, rst,
    input wire cmd_valid,
    output wire cmd_ready,
    input wire [1:0] cmd_op,
    input wire [KEY_W-1:0] cmd_slot,
    input wire [EPOCH_W-1:0] cmd_epoch,
    input wire cmd_enable,
    input wire [TOKEN_W-1:0] cmd_token,
    input wire [159:0] cmd_request,
    input wire [31:0] cmd_ttl,
    output wire rsp_valid,
    input wire rsp_ready,
    output logic [1:0] rsp_op,
    output logic [KEY_W-1:0] rsp_slot,
    output logic [3:0] rsp_status,
    output logic [EPOCH_W-1:0] rsp_epoch,
    output logic rsp_enabled,
    output logic [TOKEN_W-1:0] rsp_token,
    output logic [2:0] rsp_state,
    output logic [159:0] rsp_request,
    output logic [WINDOW-1:0] rsp_coverage,
    output logic [31:0] rsp_ttl,

    input wire descriptor_valid,
    output wire descriptor_ready, descriptor_pass,
    input wire [KEY_W-1:0] packet_slot,
    input wire [EPOCH_W-1:0] packet_epoch,
    input wire [TOKEN_W-1:0] packet_token,
    input wire [79:0] packet_session,
    input wire [63:0] packet_sequence,
    input wire [15:0] packet_count, packet_bytes,
    input wire [3:0] frame_status,
    input wire replay_active,

    output wire audit_valid,
    input wire audit_ready,
    output logic [KEY_W-1:0] audit_slot,
    output logic [EPOCH_W-1:0] audit_epoch,
    output logic [TOKEN_W-1:0] audit_token,
    output logic [3:0] audit_status,
    output logic [63:0] audit_sequence,
    output logic [15:0] audit_count, audit_bytes,
    output logic [WINDOW-1:0] audit_coverage,
    output logic audit_completed,

    input wire [KEY_W-1:0] stat_slot,
    input wire [4:0] stat_index,
    output wire stat_valid,
    output logic [31:0] stat_value,
    output logic [31:0] total_frames, rejected_frames, unknown_slots
);
    localparam integer STAT_CODES = 21;
    localparam integer OFFSET_W = WINDOW > 1 ? $clog2(WINDOW) : 1;
    localparam integer MASK_COUNT_W = $clog2(WINDOW+1);
    logic [SLOTS-1:0] enabled;
    logic [EPOCH_W-1:0] epoch [0:SLOTS-1];
    logic [TOKEN_W-1:0] token [0:SLOTS-1];
    logic [79:0] session_id [0:SLOTS-1];
    logic [63:0] first_sequence [0:SLOTS-1];
    logic [15:0] requested_count [0:SLOTS-1];
    logic [WINDOW-1:0] covered [0:SLOTS-1];
    logic [31:0] ttl [0:SLOTS-1];
    logic [2:0] lease [0:SLOTS-1];
    logic [31:0] counts [0:SLOTS-1][0:STAT_CODES-1];
    logic reply_pending, audit_pending;

    wire cmd_key_ok = int'(cmd_slot) < SLOTS;
    wire descriptor_key_ok = int'(packet_slot) < SLOTS;
    wire [SLOT_W-1:0] command_slot = cmd_key_ok ? SLOT_W'(cmd_slot) : '0;
    wire [SLOT_W-1:0] candidate_slot = descriptor_key_ok ? SLOT_W'(packet_slot) : '0;
    wire [79:0] command_session = cmd_request[159:80];
    wire [63:0] command_sequence = cmd_request[79:16];
    wire [15:0] command_count = cmd_request[15:0];
    wire fresh_epoch = cmd_key_ok && cmd_epoch > epoch[command_slot];
    wire same_epoch = cmd_epoch == epoch[command_slot];
    wire same_context = enabled[command_slot] == cmd_enable &&
                        command_session == session_id[command_slot];
    wire same_span = command_sequence == first_sequence[command_slot] &&
                     command_count == requested_count[command_slot];
    wire replay_fence = cmd_op == `MRG_SYNC && fresh_epoch &&
                        replay_active && cmd_slot == packet_slot;

    wire [63:0] delta = packet_sequence - first_sequence[candidate_slot];
    wire [64:0] packet_end = {1'b0,delta} + 65'(packet_count);
    wire in_range = packet_count != 0 && packet_end <= 65'(requested_count[candidate_slot]);
    wire [WINDOW-1:0] packet_mask = in_range ? low_mask(packet_count) << OFFSET_W'(delta) : '0;
    wire [WINDOW-1:0] next_coverage = covered[candidate_slot] | packet_mask;
    wire all_covered = next_coverage == low_mask(requested_count[candidate_slot]);
    logic [3:0] disposition, command_result;

    assign descriptor_ready = !rst && (!audit_pending || audit_ready);
    assign descriptor_pass = disposition == `MRG_ACCEPT;
    wire decide = descriptor_valid && descriptor_ready;
    wire admit = decide && descriptor_pass;
    wire complete = admit && all_covered;

    assign rsp_valid = !rst && reply_pending;
    assign audit_valid = !rst && audit_pending;
    assign cmd_ready = !rst && !decide && !replay_fence && (!reply_pending || rsp_ready);
    wire command = cmd_valid && cmd_ready;
    wire sync_commit = command && cmd_op == `MRG_SYNC && command_result == `MRG_OK && fresh_epoch;
    wire arm_commit = command && cmd_op == `MRG_ARM && command_result == `MRG_OK;
    wire cancel_commit = command && cmd_op == `MRG_CANCEL && command_result == `MRG_OK;
    wire renew_rejected = command && cmd_op == `MRG_ARM && command_result == `MRG_SAME_REQ;

    function automatic logic [WINDOW-1:0] low_mask(input logic [15:0] count);
        if (count == 0)
            low_mask = '0;
        else if (count >= 16'(WINDOW))
            low_mask = '1;
        else
            low_mask = {WINDOW{1'b1}} >> (MASK_COUNT_W'(WINDOW) - MASK_COUNT_W'(count));
    endfunction

    function automatic logic [31:0] sat_add(input logic [31:0] value, amount);
        logic [32:0] sum;
        begin
            sum = {1'b0,value} + {1'b0,amount};
            sat_add = sum[32] ? '1 : sum[31:0];
        end
    endfunction

    always_comb begin
        disposition = frame_status;
        if (frame_status == `MRG_ACCEPT) begin
            if (!descriptor_key_ok)
                disposition = `MRG_SLOT;
            else if (!enabled[candidate_slot])
                disposition = `MRG_CONTEXT;
            else if (packet_epoch != epoch[candidate_slot])
                disposition = `MRG_EPOCH;
            else if (packet_session != session_id[candidate_slot])
                disposition = `MRG_SESSION;
            else if (lease[candidate_slot] == `MRG_IDLE)
                disposition = `MRG_UNARMED;
            else if (packet_token != token[candidate_slot])
                disposition = `MRG_TOKEN;
            else if (lease[candidate_slot] == `MRG_TIMED_OUT)
                disposition = `MRG_EXPIRED;
            else if (lease[candidate_slot] == `MRG_REVOKED)
                disposition = `MRG_CANCELLED;
            else if (!in_range)
                disposition = `MRG_RANGE;
            else if ((covered[candidate_slot] & packet_mask) == packet_mask)
                disposition = `MRG_DUPLICATE;
            else if ((covered[candidate_slot] & packet_mask) != 0)
                disposition = `MRG_OVERLAP;
        end
    end

    always_comb begin
        command_result = `MRG_OK;
        if (!cmd_key_ok) begin
            command_result = `MRG_BAD_SLOT;
        end else begin
            case (cmd_op)
                `MRG_SYNC: if (cmd_epoch == 0 || (!fresh_epoch && !(same_epoch && same_context)))
                    command_result = `MRG_BAD_EPOCH;
                `MRG_ARM: begin
                    if (!same_epoch)
                        command_result = `MRG_BAD_EPOCH;
                    else if (!enabled[command_slot])
                        command_result = `MRG_DISABLED;
                    else if (command_session != session_id[command_slot] || command_count == 0 ||
                             command_count > 16'(WINDOW) || cmd_ttl == 0)
                        command_result = `MRG_BAD_REQ;
                    else if (lease[command_slot] == `MRG_LIVE)
                        command_result = same_span ? `MRG_SAME_REQ : `MRG_BUSY;
                    else if (&token[command_slot])
                        command_result = `MRG_EXHAUSTED;
                end
                `MRG_CANCEL: begin
                    if (!same_epoch)
                        command_result = `MRG_BAD_EPOCH;
                    else if (lease[command_slot] != `MRG_LIVE)
                        command_result = `MRG_NO_LEASE;
                    else if (cmd_token != token[command_slot])
                        command_result = `MRG_BAD_TOKEN;
                end
                default: begin end  // INSPECT returns the installed state.
            endcase
        end
    end

    wire stat_key_ok = int'(stat_slot) < SLOTS && int'(stat_index) < STAT_CODES;
    assign stat_valid = !rst && stat_key_ok;
    always_comb begin
        stat_value = '0;
        if (stat_valid)
            stat_value = counts[SLOT_W'(stat_slot)][stat_index];
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            reply_pending <= 0;
            rsp_op <= '0;
            rsp_slot <= '0;
            rsp_status <= '0;
            rsp_epoch <= '0;
            rsp_enabled <= 0;
            rsp_token <= '0;
            rsp_state <= '0;
            rsp_request <= '0;
            rsp_coverage <= '0;
            rsp_ttl <= '0;
        end else begin
            if (rsp_ready)
                reply_pending <= 0;
            if (command) begin
                reply_pending <= 1;
                rsp_op <= cmd_op;
                rsp_slot <= cmd_slot;
                rsp_status <= command_result;
                rsp_epoch <= sync_commit ? cmd_epoch : (cmd_key_ok ? epoch[command_slot] : '0);
                rsp_enabled <= sync_commit ? cmd_enable : (cmd_key_ok && enabled[command_slot]);
                rsp_token <= !cmd_key_ok || sync_commit ? '0 :
                             arm_commit ? token[command_slot] + 1'b1 : token[command_slot];
                rsp_state <= !cmd_key_ok || sync_commit ? `MRG_IDLE :
                             arm_commit ? `MRG_LIVE : cancel_commit ? `MRG_REVOKED : lease[command_slot];
                rsp_request <= !cmd_key_ok ? '0 :
                               sync_commit ? {command_session,80'd0} :
                               arm_commit ? cmd_request : {session_id[command_slot],
                                                           first_sequence[command_slot],
                                                           requested_count[command_slot]};
                rsp_coverage <= !cmd_key_ok || sync_commit || arm_commit ? '0 : covered[command_slot];
                rsp_ttl <= !cmd_key_ok || sync_commit || cancel_commit ? '0 :
                           arm_commit ? cmd_ttl : ttl[command_slot];
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            audit_pending <= 0;
            audit_slot <= '0;
            audit_epoch <= '0;
            audit_token <= '0;
            audit_status <= '0;
            audit_sequence <= '0;
            audit_count <= '0;
            audit_bytes <= '0;
            audit_coverage <= '0;
            audit_completed <= 0;
            total_frames <= '0;
            rejected_frames <= '0;
            unknown_slots <= '0;
        end else begin
            if (audit_ready)
                audit_pending <= 0;
            if (decide) begin
                audit_pending <= 1;
                audit_slot <= packet_slot;
                audit_epoch <= packet_epoch;
                audit_token <= packet_token;
                audit_status <= disposition;
                audit_sequence <= packet_sequence;
                audit_count <= packet_count;
                audit_bytes <= packet_bytes;
                audit_coverage <= admit ? next_coverage : '0;
                audit_completed <= complete;
                total_frames <= sat_add(total_frames,32'd1);
                if (!admit)
                    rejected_frames <= sat_add(rejected_frames,32'd1);
                if (!descriptor_key_ok)
                    unknown_slots <= sat_add(unknown_slots,32'd1);
            end
        end
    end

    for (genvar slot = 0; slot < SLOTS; slot++) begin : state_for_slot
        localparam logic [SLOT_W-1:0] SLOT = SLOT_W'(slot);
        wire install = sync_commit && command_slot == SLOT;
        wire arm = arm_commit && command_slot == SLOT;
        wire cancel = cancel_commit && command_slot == SLOT;
        wire sample = decide && descriptor_key_ok && candidate_slot == SLOT;
        wire finish_entry = complete && candidate_slot == SLOT;
        wire expires = !install && !cancel && !finish_entry && lease[slot] == `MRG_LIVE && ttl[slot] == 1;

        always_ff @(posedge clk) begin
            if (rst) begin
                enabled[slot] <= 0;
                epoch[slot] <= '0;
                session_id[slot] <= '0;
            end else if (install) begin
                enabled[slot] <= cmd_enable;
                epoch[slot] <= cmd_epoch;
                session_id[slot] <= command_session;
            end
        end

        always_ff @(posedge clk) begin
            if (rst || install) begin
                token[slot] <= '0;
                first_sequence[slot] <= '0;
                requested_count[slot] <= '0;
                covered[slot] <= '0;
                ttl[slot] <= '0;
                lease[slot] <= `MRG_IDLE;
            end else if (arm) begin
                token[slot] <= token[slot] + 1'b1;
                first_sequence[slot] <= command_sequence;
                requested_count[slot] <= command_count;
                covered[slot] <= '0;
                ttl[slot] <= cmd_ttl;
                lease[slot] <= `MRG_LIVE;
            end else if (cancel) begin
                ttl[slot] <= '0;
                lease[slot] <= `MRG_REVOKED;
            end else if (lease[slot] == `MRG_LIVE) begin
                if (sample && admit)
                    covered[slot] <= next_coverage;
                if (finish_entry) begin
                    ttl[slot] <= '0;
                    lease[slot] <= `MRG_COMPLETE;
                end else begin
                    ttl[slot] <= ttl[slot] - 1'b1;
                    if (expires)
                        lease[slot] <= `MRG_TIMED_OUT;
                end
            end
        end

        for (genvar code = 0; code < 15; code++) begin : decision_counts
            always_ff @(posedge clk) begin
                if (rst)
                    counts[slot][code] <= '0;
                else if (sample && disposition == 4'(code))
                    counts[slot][code] <= sat_add(counts[slot][code],32'd1);
            end
        end
        always_ff @(posedge clk) begin
            if (rst) begin
                counts[slot][15] <= '0;
                counts[slot][16] <= '0;
                counts[slot][17] <= '0;
                counts[slot][18] <= '0;
                counts[slot][19] <= '0;
                counts[slot][20] <= '0;
            end else begin
                if (sample && admit)
                    counts[slot][15] <= sat_add(counts[slot][15],32'(packet_count));
                if (arm)
                    counts[slot][16] <= sat_add(counts[slot][16],32'd1);
                if (cancel)
                    counts[slot][17] <= sat_add(counts[slot][17],32'd1);
                if (expires)
                    counts[slot][18] <= sat_add(counts[slot][18],32'd1);
                if (renew_rejected && command_slot == SLOT)
                    counts[slot][19] <= sat_add(counts[slot][19],32'd1);
                if (install)
                    counts[slot][20] <= sat_add(counts[slot][20],32'd1);
            end
        end
    end
endmodule

`default_nettype wire
