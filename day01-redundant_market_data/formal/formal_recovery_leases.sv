`default_nettype none

module formal_recovery_leases;
    localparam integer SLOTS = 2, KEY_W = 2, EPOCH_W = 3, TOKEN_W = 2, WINDOW = 4;
    localparam logic [79:0] A = "CHANNEL321", B = "CHANNEL654";
    (* gclk *) logic clk;
    (* anyseq *) logic rst, cmd_valid, cmd_enable, rsp_ready, descriptor_valid, audit_ready;
    (* anyseq *) logic [1:0] cmd_op;
    (* anyseq *) logic [KEY_W-1:0] cmd_slot, packet_slot;
    (* anyseq *) logic [EPOCH_W-1:0] cmd_epoch, packet_epoch;
    (* anyseq *) logic [TOKEN_W-1:0] cmd_token, packet_token;
    (* anyseq *) logic [3:0] frame_status;
    (* anyseq *) logic [2:0] requested, delivered, requested_ttl, request_head, packet_head;
    (* anyseq *) logic request_session, response_session;
    (* anyseq *) logic replay_active;
    wire [159:0] cmd_request = {
        request_session ? A : B,61'd0,request_head,13'd0,requested
    };
    wire [31:0] cmd_ttl = 32'(requested_ttl);
    wire [79:0] packet_session = response_session ? A : B;
    wire [63:0] packet_sequence = 64'(packet_head);
    wire [15:0] packet_count = 16'(delivered), packet_bytes = 16'd48;
    wire [KEY_W-1:0] stat_slot = 0;
    wire [4:0] stat_index = 15;
    wire cmd_ready, rsp_valid, descriptor_ready, descriptor_pass, audit_valid, audit_completed;
    wire rsp_enabled, stat_valid;
    wire [1:0] rsp_op;
    wire [KEY_W-1:0] rsp_slot, audit_slot;
    wire [EPOCH_W-1:0] rsp_epoch, audit_epoch;
    wire [TOKEN_W-1:0] rsp_token, audit_token;
    wire [3:0] rsp_status, audit_status;
    wire [2:0] rsp_state;
    wire [WINDOW-1:0] rsp_coverage, audit_coverage;
    wire [159:0] rsp_request;
    wire [31:0] rsp_ttl, stat_value, total_frames, rejected_frames, unknown_slots;
    wire [63:0] audit_sequence;
    wire [15:0] audit_count, audit_bytes;

    mold_recovery_leases #(.SLOTS(SLOTS), .WINDOW(WINDOW), .EPOCH_W(EPOCH_W), .TOKEN_W(TOKEN_W))
        dut (.*);

    logic past_valid = 0;
    wire decide = descriptor_valid && descriptor_ready;
    wire control = cmd_valid && cmd_ready;
    always @(posedge clk) begin
        past_valid <= 1;
        if (!past_valid)
            assume(rst);
        assume(frame_status < 4);
        if (rst)
            assert(!cmd_ready && !rsp_valid && !audit_valid && !descriptor_ready && !stat_valid);
        if (past_valid && !$past(rst) && !rst) begin
            if ($past(rsp_valid && !rsp_ready)) begin
                assert(rsp_valid);
                assert($stable({rsp_op,rsp_slot,rsp_status,rsp_epoch,rsp_enabled,
                                rsp_token,rsp_state,rsp_request,rsp_coverage,rsp_ttl}));
            end
            if ($past(audit_valid && !audit_ready)) begin
                assert(audit_valid);
                assert($stable({audit_slot,audit_epoch,audit_token,audit_status,
                                audit_sequence,audit_count,audit_bytes,audit_coverage,audit_completed}));
            end
            if ($past(decide)) begin
                assert(audit_valid);
                assert({audit_slot,audit_epoch,audit_token,audit_sequence,audit_count,audit_bytes} ==
                       $past({packet_slot,packet_epoch,packet_token,packet_sequence,packet_count,packet_bytes}));
                assert((audit_status == 0) == $past(descriptor_pass));
                if ($past(frame_status != 0))
                    assert(audit_status == $past(frame_status));
                if ($past(packet_slot >= SLOTS && frame_status == 0))
                    assert(audit_status == 4);
                assert(total_frames == $past(total_frames) + 1);
            end
            if (audit_valid && audit_status == 0) begin
                assert(audit_epoch != 0 && audit_token != 0);
                assert(audit_count != 0 && audit_count <= WINDOW && audit_coverage != 0);
            end
            if (audit_valid && audit_status != 0)
                assert(!audit_completed && audit_coverage == 0);
            if ($past(control)) begin
                assert(rsp_valid && rsp_slot == $past(cmd_slot) && rsp_op == $past(cmd_op));
                if ($past(cmd_op == 1) && rsp_status == 0) begin
                    assert(rsp_epoch == $past(cmd_epoch) && rsp_token != 0 && rsp_state == 1);
                    assert(rsp_request == $past(cmd_request) && rsp_ttl == $past(cmd_ttl));
                    assert(rsp_coverage == 0);
                end
            end
            assert(!control || !decide);
            assert(rejected_frames <= total_frames && unknown_slots <= total_frames);
            cover(audit_valid && audit_completed);
            cover(audit_valid && audit_status == 12);
            cover(audit_valid && audit_status == 13);
            cover(audit_valid && audit_status == 10);
            cover(audit_valid && audit_status == 14);
            cover(replay_active && cmd_valid && cmd_op == 0 && cmd_slot == packet_slot &&
                  !cmd_ready && rsp_ready && !decide);
        end
    end
endmodule

`default_nettype wire
