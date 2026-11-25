`timescale 1ns/1ps
`default_nettype none
`include "mold_recovery_codes.svh"

// Bridge receiver-originated recovery requests to the guard's ARM channel.
// A separate admin port keeps context changes and policy barriers explicit.
module mold_recovery_dispatch #(
    parameter integer SLOTS = 3,
    parameter integer EPOCH_W = 16,
    parameter integer TOKEN_W = 16,
    parameter integer WINDOW = 16,
    localparam integer KEY_W = $clog2(SLOTS+1),
    localparam integer ADMIN_W = 202+KEY_W+EPOCH_W+TOKEN_W+WINDOW
) (
    input wire clk, rst,

    input wire source_valid,
    output wire source_ready,
    input wire [KEY_W-1:0] source_slot,
    input wire [EPOCH_W-1:0] source_epoch,
    input wire [159:0] source_request,
    input wire [31:0] source_ttl,
    input wire source_resend,

    input wire admin_valid,
    output wire admin_ready,
    input wire [1:0] admin_op,
    input wire [KEY_W-1:0] admin_slot,
    input wire [EPOCH_W-1:0] admin_epoch,
    input wire admin_enable,
    input wire [TOKEN_W-1:0] admin_token,
    input wire [159:0] admin_request,
    input wire [31:0] admin_ttl,
    output wire admin_reply_valid,
    input wire admin_reply_ready,
    output logic [ADMIN_W-1:0] admin_reply,

    output wire tx_valid,
    input wire tx_ready,
    output logic tx_retry,
    output logic [KEY_W-1:0] tx_slot,
    output logic [EPOCH_W-1:0] tx_epoch,
    output logic [TOKEN_W-1:0] tx_token,
    output logic [159:0] tx_request,

    output wire decision_valid,
    input wire decision_ready,
    output logic [KEY_W-1:0] decision_slot,
    output logic [EPOCH_W-1:0] decision_epoch,
    output logic [TOKEN_W-1:0] decision_token,
    output logic [3:0] decision_status,
    output logic [1:0] decision_action,
    output logic [31:0] decision_ttl,

    output wire cmd_valid,
    input wire cmd_ready,
    output logic [1:0] cmd_op,
    output logic [KEY_W-1:0] cmd_slot,
    output logic [EPOCH_W-1:0] cmd_epoch,
    output logic cmd_enable,
    output logic [TOKEN_W-1:0] cmd_token,
    output logic [159:0] cmd_request,
    output logic [31:0] cmd_ttl,
    input wire rsp_valid,
    output wire rsp_ready,
    input wire [1:0] rsp_op,
    input wire [KEY_W-1:0] rsp_slot,
    input wire [3:0] rsp_status,
    input wire [EPOCH_W-1:0] rsp_epoch,
    input wire rsp_enabled,
    input wire [TOKEN_W-1:0] rsp_token,
    input wire [2:0] rsp_state,
    input wire [159:0] rsp_request,
    input wire [WINDOW-1:0] rsp_coverage,
    input wire [31:0] rsp_ttl,

    output logic [31:0] issued_requests, repeated_requests, suppressed_requests,
    output logic [31:0] rejected_requests, bad_responses
);
    localparam logic [1:0] REJECT = 0, NEW_REQUEST = 1, REPEAT = 2, SUPPRESS = 3;
    localparam logic [3:0] BAD_REPLY = 15;
    localparam integer SOURCE_W = KEY_W+EPOCH_W+193;
    localparam integer COMMAND_W = KEY_W+EPOCH_W+TOKEN_W+195;
    localparam logic [1:0] IDLE = 0, COMMAND = 1, RESPONSE = 2;
    logic [1:0] state;

    logic source_pending, admin_pending, tx_pending, decision_pending, admin_reply_pending;
    logic prefer_admin, active_admin, allow_resend;
    logic [SOURCE_W-1:0] source_queue;
    logic [COMMAND_W-1:0] admin_queue;
    wire [KEY_W-1:0] pending_source_slot, pending_admin_slot;
    wire [EPOCH_W-1:0] pending_source_epoch, pending_admin_epoch;
    wire [159:0] pending_source_request, pending_admin_request;
    wire [31:0] pending_source_ttl, pending_admin_ttl;
    wire pending_resend, pending_admin_enable;
    wire [1:0] pending_admin_op;
    wire [TOKEN_W-1:0] pending_admin_token;
    assign {pending_source_slot,pending_source_epoch,pending_source_request,
            pending_source_ttl,pending_resend} = source_queue;
    assign {pending_admin_op,pending_admin_slot,pending_admin_epoch,pending_admin_enable,
            pending_admin_token,pending_admin_request,pending_admin_ttl} = admin_queue;

    wire admin_fenced = tx_pending && tx_slot == pending_admin_slot &&
                        (pending_admin_op == `MRG_SYNC || pending_admin_op == `MRG_CANCEL);
    wire choose_source = source_pending && !tx_pending && !decision_pending;
    wire choose_admin = admin_pending && !admin_fenced && !admin_reply_pending;
    wire same_slot_barrier = pending_admin_slot == pending_source_slot &&
                             (pending_admin_op == `MRG_SYNC || pending_admin_op == `MRG_CANCEL);
    wire take_admin = state == IDLE && choose_admin &&
                      (!choose_source || prefer_admin || same_slot_barrier);
    wire take_source = state == IDLE && choose_source && !take_admin;
    wire take_command = cmd_valid && cmd_ready;
    wire take_response = rsp_valid && rsp_ready;
    wire response_is_request = !active_admin &&
                               (rsp_status == `MRG_OK || rsp_status == `MRG_SAME_REQ);
    wire bad_reply = rsp_op != cmd_op || rsp_slot != cmd_slot ||
                     (response_is_request && (rsp_epoch != cmd_epoch || rsp_state != `MRG_LIVE ||
                                              rsp_token == 0 || rsp_request != cmd_request ||
                                              rsp_ttl == 0 || !rsp_enabled));
    wire repeat_request = response_is_request && rsp_status == `MRG_SAME_REQ;
    wire forward_request = response_is_request && !bad_reply && (!repeat_request || allow_resend);
    wire tx_transfer = tx_valid && tx_ready;

    assign source_ready = !rst && !source_pending;
    assign admin_ready = !rst && !admin_pending;
    assign tx_valid = !rst && tx_pending;
    assign decision_valid = !rst && decision_pending;
    assign admin_reply_valid = !rst && admin_reply_pending;
    assign cmd_valid = !rst && state == COMMAND;
    assign rsp_ready = !rst && state == RESPONSE;

    function automatic logic [31:0] inc(input logic [31:0] value);
        inc = (&value) ? value : value + 1'b1;
    endfunction

    always_ff @(posedge clk) begin
        if (rst) begin
            source_pending <= 0;
            admin_pending <= 0;
            source_queue <= '0;
            admin_queue <= '0;
            state <= IDLE;
            prefer_admin <= 1;
            active_admin <= 0;
            allow_resend <= 0;
            {cmd_op,cmd_slot,cmd_epoch,cmd_enable,cmd_token,cmd_request,cmd_ttl} <= '0;
        end else begin
            if (source_valid && source_ready) begin
                source_pending <= 1;
                source_queue <= {source_slot,source_epoch,source_request,source_ttl,source_resend};
            end else if (take_source) begin
                source_pending <= 0;
            end
            if (admin_valid && admin_ready) begin
                admin_pending <= 1;
                admin_queue <= {admin_op,admin_slot,admin_epoch,admin_enable,
                                admin_token,admin_request,admin_ttl};
            end else if (take_admin) begin
                admin_pending <= 0;
            end
            if (take_admin || take_source) begin
                state <= COMMAND;
                active_admin <= take_admin;
                prefer_admin <= take_source;
                allow_resend <= take_source && pending_resend;
                if (take_admin) begin
                    {cmd_op,cmd_slot,cmd_epoch,cmd_enable,cmd_token,cmd_request,cmd_ttl} <= {
                        pending_admin_op,pending_admin_slot,pending_admin_epoch,pending_admin_enable,
                        pending_admin_token,pending_admin_request,pending_admin_ttl
                    };
                end else begin
                    cmd_op <= `MRG_ARM;
                    cmd_slot <= pending_source_slot;
                    cmd_epoch <= pending_source_epoch;
                    cmd_enable <= 1;
                    cmd_token <= '0;
                    cmd_request <= pending_source_request;
                    cmd_ttl <= pending_source_ttl;
                end
            end else if (take_command) begin
                state <= RESPONSE;
            end else if (take_response) begin
                state <= IDLE;
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            tx_pending <= 0;
            decision_pending <= 0;
            admin_reply_pending <= 0;
            admin_reply <= '0;
            {tx_retry,tx_slot,tx_epoch,tx_token,tx_request} <= '0;
            {decision_slot,decision_epoch,decision_token,decision_status,
             decision_action,decision_ttl} <= '0;
            issued_requests <= '0;
            repeated_requests <= '0;
            suppressed_requests <= '0;
            rejected_requests <= '0;
            bad_responses <= '0;
        end else begin
            if (tx_transfer) begin
                tx_pending <= 0;
                issued_requests <= inc(issued_requests);
                if (tx_retry)
                    repeated_requests <= inc(repeated_requests);
            end
            if (decision_ready)
                decision_pending <= 0;
            if (admin_reply_ready)
                admin_reply_pending <= 0;
            if (take_response) begin
                if (bad_reply)
                    bad_responses <= inc(bad_responses);
                if (active_admin) begin
                    admin_reply_pending <= 1;
                    admin_reply <= {rsp_op,bad_reply ? BAD_REPLY : rsp_status,rsp_slot,
                                    rsp_epoch,rsp_token,rsp_enabled,rsp_state,rsp_request,
                                    rsp_coverage,rsp_ttl};
                end else begin
                    decision_pending <= 1;
                    decision_slot <= cmd_slot;
                    decision_epoch <= cmd_epoch;
                    decision_token <= bad_reply ? '0 : rsp_token;
                    decision_status <= bad_reply ? BAD_REPLY : rsp_status;
                    decision_ttl <= bad_reply ? '0 : rsp_ttl;
                    decision_action <= !response_is_request || bad_reply ? REJECT :
                                       !repeat_request ? NEW_REQUEST : allow_resend ? REPEAT : SUPPRESS;
                    if (forward_request) begin
                        tx_pending <= 1;
                        {tx_retry,tx_slot,tx_epoch,tx_token,tx_request} <= {
                            repeat_request,cmd_slot,cmd_epoch,rsp_token,cmd_request
                        };
                    end else if (response_is_request && !bad_reply) begin
                        suppressed_requests <= inc(suppressed_requests);
                    end else begin
                        rejected_requests <= inc(rejected_requests);
                    end
                end
            end
        end
    end
endmodule

`default_nettype wire
