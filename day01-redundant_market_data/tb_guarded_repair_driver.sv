`timescale 1ns/1ps
`default_nettype none

// Exercise the guard with actual receiver-originated wire requests, then put
// admitted bytes on the receiver lane alongside independent live datagrams.
module tb_guarded_repair_driver #(
    parameter integer EPOCH_W = 3,
    parameter integer KEY_W = 2
) (
    input wire clk, rst, execute,
    output logic complete = 0,
    input wire [159:0] request_a, request_b,

    input wire live_valid,
    output wire live_ready,
    input wire [63:0] live_data,
    input wire [7:0] live_keep,
    input wire live_first, live_last,
    input wire [KEY_W-1:0] live_slot,

    output wire output_valid,
    input wire output_ready,
    output wire [63:0] output_data,
    output wire [7:0] output_keep,
    output wire output_first, output_last,
    output wire [KEY_W-1:0] output_slot
);
    localparam integer TOKEN_W = 4;
    localparam integer WINDOW = 8;
    logic admin_valid = 0, source_valid = 0, repair_valid = 0;
    logic [1:0] admin_op;
    wire [1:0] cmd_op, rsp_op;
    logic [KEY_W-1:0] admin_slot, source_slot, repair_slot;
    wire [KEY_W-1:0] cmd_slot, rsp_slot, tx_slot;
    logic [EPOCH_W-1:0] repair_epoch;
    wire [EPOCH_W-1:0] cmd_epoch, tx_epoch;
    logic [TOKEN_W-1:0] repair_token;
    logic [159:0] admin_request, source_request;
    wire [159:0] cmd_request, rsp_request, tx_request;
    logic [63:0] repair_data;
    logic [7:0] repair_keep;
    logic repair_first, repair_last;
    wire cmd_valid, cmd_ready, cmd_enable, repair_ready, rsp_valid, rsp_ready, rsp_enabled;
    wire guard_valid, guard_ready, admin_ready, admin_reply_valid, source_ready, tx_valid;
    wire tx_retry;
    wire [TOKEN_W-1:0] cmd_token, tx_token;
    wire [31:0] cmd_ttl, rsp_ttl, issued_requests;
    wire [WINDOW-1:0] rsp_coverage;
    wire [202+KEY_W+EPOCH_W+TOKEN_W+WINDOW-1:0] admin_reply;
    wire [3:0] rsp_status, audit_status;
    wire [2:0] rsp_state;
    wire [TOKEN_W-1:0] rsp_token, audit_token;
    wire [EPOCH_W-1:0] rsp_epoch, audit_epoch;
    wire [KEY_W-1:0] guard_slot, audit_slot;
    wire [63:0] guard_data;
    wire [7:0] guard_keep;
    wire guard_first, guard_last, audit_valid, audit_completed;
    wire [15:0] audit_count;
    wire [WINDOW-1:0] audit_coverage;
    wire [31:0] total_frames, rejected_frames, unknown_slots;

    mold_recovery_dispatch #(.SLOTS(3), .WINDOW(WINDOW), .EPOCH_W(EPOCH_W), .TOKEN_W(TOKEN_W)) dispatch (
        .clk, .rst, .source_valid, .source_ready, .source_slot, .source_epoch(EPOCH_W'(2)),
        .source_request, .source_ttl(32'd4096), .source_resend(1'b0),
        .admin_valid, .admin_ready, .admin_op, .admin_slot, .admin_epoch(EPOCH_W'(2)),
        .admin_enable(1'b1), .admin_token(TOKEN_W'(0)), .admin_request, .admin_ttl(32'd0),
        .admin_reply_valid, .admin_reply_ready(1'b1), .admin_reply,
        .tx_valid, .tx_ready(1'b1), .tx_retry, .tx_slot, .tx_epoch, .tx_token, .tx_request,
        .decision_valid(), .decision_ready(1'b1), .decision_slot(), .decision_epoch(), .decision_token(),
        .decision_status(), .decision_action(), .decision_ttl(),
        .cmd_valid, .cmd_ready, .cmd_op, .cmd_slot, .cmd_epoch, .cmd_enable, .cmd_token,
        .cmd_request, .cmd_ttl, .rsp_valid, .rsp_ready, .rsp_op, .rsp_slot, .rsp_status,
        .rsp_epoch, .rsp_enabled, .rsp_token, .rsp_state, .rsp_request, .rsp_coverage, .rsp_ttl,
        .issued_requests, .repeated_requests(), .suppressed_requests(), .rejected_requests(),
        .bad_responses()
    );

    mold_recovery_guard #(
        .SLOTS(3), .PACKET_BYTES(128), .WINDOW(WINDOW), .EPOCH_W(EPOCH_W), .TOKEN_W(TOKEN_W)
    ) guard (
        .clk, .rst, .cmd_valid, .cmd_ready, .cmd_op, .cmd_slot, .cmd_epoch,
        .cmd_enable, .cmd_token, .cmd_request, .cmd_ttl,
        .rsp_valid, .rsp_ready, .rsp_op, .rsp_slot, .rsp_status, .rsp_epoch,
        .rsp_enabled, .rsp_token, .rsp_state, .rsp_request, .rsp_coverage, .rsp_ttl,
        .rx_valid(repair_valid), .rx_ready(repair_ready), .rx_data(repair_data),
        .rx_keep(repair_keep), .rx_first(repair_first), .rx_last(repair_last),
        .rx_slot(repair_slot), .rx_epoch(repair_epoch), .rx_token(repair_token),
        .replay_valid(guard_valid), .replay_ready(guard_ready), .replay_data(guard_data),
        .replay_keep(guard_keep), .replay_first(guard_first), .replay_last(guard_last),
        .replay_slot(guard_slot), .replay_epoch(), .replay_token(),
        .audit_valid, .audit_ready(1'b1), .audit_slot, .audit_epoch, .audit_token, .audit_status,
        .audit_sequence(), .audit_count, .audit_bytes(), .audit_coverage, .audit_completed,
        .stat_slot(KEY_W'(0)), .stat_index(5'd0), .stat_valid(), .stat_value(),
        .total_frames, .rejected_frames, .unknown_slots
    );

    mold_repair_lane_mux #(.KEY_W(KEY_W)) receiver_lane (
        .clk, .rst, .s_valid({guard_valid,live_valid}), .s_ready({guard_ready,live_ready}),
        .s_data({guard_data,live_data}), .s_keep({guard_keep,live_keep}),
        .s_first({guard_first,live_first}), .s_last({guard_last,live_last}),
        .s_slot({guard_slot,live_slot}), .m_valid(output_valid), .m_ready(output_ready),
        .m_data(output_data), .m_keep(output_keep), .m_first(output_first), .m_last(output_last),
        .m_slot(output_slot)
    );

    integer passed = 0, completions = 0, lane_contentions = 0;
    integer replay_transfers = 0;
    logic own_mux_lease = 0;
    logic previous_owner;
    always @(posedge clk) begin
        if (rst) begin
            own_mux_lease = 0;
        end else begin
            if (guard_valid && live_valid)
                lane_contentions++;
            if (guard_valid && guard_ready && guard_last)
                replay_transfers++;
            if (output_valid && output_ready) begin
                if (output_first == own_mux_lease)
                    $fatal(1,"physical lane split two frames during arbitration");
                if (own_mux_lease && receiver_lane.selected != previous_owner)
                    $fatal(1,"physical lane changed owner inside a frame");
                own_mux_lease = !output_last;
                previous_owner = receiver_lane.selected;
            end
        end
    end

    task automatic install_slot(input logic [KEY_W-1:0] slot, input logic [159:0] request);
        @(negedge clk);
        admin_valid = 1;
        admin_op = 0;
        admin_slot = slot;
        admin_request = request;
        do @(posedge clk); while (!admin_ready);
        @(negedge clk);
        admin_valid = 0;
        while (!admin_reply_valid)
            @(negedge clk);
        if (admin_reply !== {2'd0,4'd0,slot,EPOCH_W'(2),TOKEN_W'(0),1'b1,3'd0,
                             request[159:80],80'd0,WINDOW'(0),32'd0})
            $fatal(1,"external recovery adapter could not configure slot %0d",slot);
    endtask

    task automatic issue_request(input logic [KEY_W-1:0] slot, input logic [159:0] request);
        @(negedge clk);
        source_valid = 1;
        source_slot = slot;
        source_request = request;
        do @(posedge clk); while (!source_ready);
        @(negedge clk);
        source_valid = 0;
        while (!tx_valid)
            @(negedge clk);
        if ({tx_retry,tx_slot,tx_epoch,tx_token,tx_request} !==
            {1'b0,slot,EPOCH_W'(2),TOKEN_W'(1),request})
            $fatal(1,"transport request did not preserve wire bytes and route token");
    endtask

    task automatic feed_frame(input integer fp);
        integer slot, epoch, token, expected_status, words, fields;
        logic [1:0] framing;
        localparam logic [WINDOW-1:0] ALL_CASE_BITS = WINDOW'(3);
        fields = $fscanf(fp,"%d %d %d %d %d",slot,epoch,token,expected_status,words);
        if (fields != 5 || words < 1 || words > 16)
            $fatal(1,"invalid guard fixture descriptor");
        for (integer word_index = 0; word_index < words; word_index++) begin
            @(negedge clk);
            fields = $fscanf(fp,"%h %h %h",repair_data,repair_keep,framing);
            if (fields != 3)
                $fatal(1,"invalid guard fixture word");
            repair_valid = 1;
            {repair_first,repair_last} = framing;
            repair_slot = KEY_W'(slot);
            repair_epoch = EPOCH_W'(epoch);
            repair_token = TOKEN_W'(token);
            do @(posedge clk); while (!repair_ready);
            @(negedge clk);
            repair_valid = 0;
        end
        while (!audit_valid)
            @(negedge clk);
        if ({audit_slot,audit_epoch,audit_token,audit_status} !==
            {KEY_W'(slot),EPOCH_W'(epoch),TOKEN_W'(token),4'(expected_status)})
            $fatal(1,"guard returned wrong disposition for %0d/%0d/%0d",slot,epoch,token);
        if (expected_status == 0) begin
            passed++;
            if (audit_count != 1 || audit_coverage != (audit_completed ? ALL_CASE_BITS : WINDOW'(2)))
                $fatal(1,"out-of-order split reply coverage did not converge");
            completions += audit_completed;
        end
    endtask

    initial begin
        string trace;
        integer file, frames;
        wait (execute);
        if (!$value$plusargs("guarded_repair=%s",trace))
            $fatal(1,"provide +guarded_repair for archive-backed inputs");
        file = $fopen(trace,"r");
        if (!file || $fscanf(file,"%d",frames) != 1 || frames != 9)
            $fatal(1,"cannot open archive-backed guard fixture");
        install_slot(KEY_W'(0),request_a);
        install_slot(KEY_W'(1),request_b);
        issue_request(KEY_W'(0),request_a);
        issue_request(KEY_W'(1),request_b);
        repeat (frames) feed_frame(file);
        $fclose(file);
        while (guard_valid)
            @(negedge clk);
        if (passed != 4 || completions != 2 || replay_transfers != 4 ||
            total_frames != 9 || rejected_frames != 5 || unknown_slots != 0 ||
            issued_requests != 2 || lane_contentions == 0)
            $fatal(1,"guarded recovery integration totals disagree");
        $display("PASS guarded archive loop: %0d admits, %0d rejects, %0d complete leases, %0d contended lane cycles",
                 passed,rejected_frames,completions,lane_contentions);
        complete = 1;
    end
endmodule

`default_nettype wire
