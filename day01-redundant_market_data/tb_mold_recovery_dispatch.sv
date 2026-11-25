`timescale 1ns/1ps
`default_nettype none

module tb_mold_recovery_dispatch;
    localparam integer SLOTS = 3, KEY_W = 2, EPOCH_W = 4, TOKEN_W = 3, WINDOW = 7;
    localparam integer ADMIN_W = 202+KEY_W+EPOCH_W+TOKEN_W+WINDOW;
    localparam logic [79:0] SESSION = "CHANNEL101";
    localparam logic [159:0] REQUEST = {SESSION,64'd310,16'd3};
    localparam logic [159:0] OTHER = {SESSION,64'd320,16'd2};

    logic clk = 0, rst = 1;
    always #5 clk = !clk;

    logic source_valid = 0, source_resend;
    logic [KEY_W-1:0] source_slot;
    logic [EPOCH_W-1:0] source_epoch;
    logic [159:0] source_request;
    logic [31:0] source_ttl = 500;
    wire source_ready;
    logic admin_valid = 0, admin_enable = 1, admin_reply_ready = 0;
    logic [1:0] admin_op;
    logic [KEY_W-1:0] admin_slot;
    logic [EPOCH_W-1:0] admin_epoch;
    logic [TOKEN_W-1:0] admin_token = 0;
    logic [159:0] admin_request = {SESSION,80'd0};
    logic [31:0] admin_ttl = 0;
    wire admin_ready, admin_reply_valid;
    wire [ADMIN_W-1:0] admin_reply;

    wire tx_valid, tx_retry;
    logic tx_ready = 0;
    wire [KEY_W-1:0] tx_slot;
    wire [EPOCH_W-1:0] tx_epoch;
    wire [TOKEN_W-1:0] tx_token;
    wire [159:0] tx_request;
    wire decision_valid;
    logic decision_ready = 0;
    wire [KEY_W-1:0] decision_slot;
    wire [EPOCH_W-1:0] decision_epoch;
    wire [TOKEN_W-1:0] decision_token;
    wire [3:0] decision_status;
    wire [1:0] decision_action;
    wire [31:0] decision_ttl;

    wire cmd_valid, cmd_enable;
    logic cmd_ready = 0;
    wire [1:0] cmd_op;
    wire [KEY_W-1:0] cmd_slot;
    wire [EPOCH_W-1:0] cmd_epoch;
    wire [TOKEN_W-1:0] cmd_token;
    wire [159:0] cmd_request;
    wire [31:0] cmd_ttl;
    logic rsp_valid = 0, rsp_enabled = 1;
    wire rsp_ready;
    logic [1:0] rsp_op;
    logic [KEY_W-1:0] rsp_slot;
    logic [3:0] rsp_status;
    logic [EPOCH_W-1:0] rsp_epoch;
    logic [TOKEN_W-1:0] rsp_token;
    logic [2:0] rsp_state;
    logic [159:0] rsp_request;
    logic [WINDOW-1:0] rsp_coverage = 0;
    logic [31:0] rsp_ttl;

    wire [31:0] issued_requests, repeated_requests, suppressed_requests;
    wire [31:0] rejected_requests, bad_responses;

    mold_recovery_dispatch #(
        .SLOTS(SLOTS), .EPOCH_W(EPOCH_W), .TOKEN_W(TOKEN_W), .WINDOW(WINDOW)
    ) dut (.*);

    typedef struct packed {
        logic retry;
        logic [KEY_W-1:0] slot;
        logic [EPOCH_W-1:0] epoch;
        logic [TOKEN_W-1:0] token;
        logic [159:0] request;
    } transport_t;
    typedef struct packed {
        logic [KEY_W-1:0] slot;
        logic [EPOCH_W-1:0] epoch;
        logic [TOKEN_W-1:0] token;
        logic [3:0] status;
        logic [1:0] action;
        logic [31:0] ttl;
    } decision_t;
    wire transport_t tx_word = {tx_retry,tx_slot,tx_epoch,tx_token,tx_request};
    wire decision_t decision_word = {
        decision_slot,decision_epoch,decision_token,decision_status,decision_action,decision_ttl
    };
    transport_t previous_tx;
    decision_t previous_decision;
    logic [ADMIN_W-1:0] previous_admin;
    logic held_tx = 0, held_decision = 0, held_admin = 0;
    integer command_edges = 0, tx_edges = 0, stalls = 0, fenced = 0;

    always @(posedge clk) begin
        if (rst) begin
            held_tx = 0;
            held_decision = 0;
            held_admin = 0;
        end else begin
            if (held_tx && (!tx_valid || tx_word !== previous_tx))
                $fatal(1,"outgoing request changed under a transport stall");
            if (held_decision && (!decision_valid || decision_word !== previous_decision))
                $fatal(1,"request disposition changed under a stall");
            if (held_admin && (!admin_reply_valid || admin_reply !== previous_admin))
                $fatal(1,"admin reply changed under a stall");
            held_tx = tx_valid && !tx_ready;
            held_decision = decision_valid && !decision_ready;
            held_admin = admin_reply_valid && !admin_reply_ready;
            previous_tx = tx_word;
            previous_decision = decision_word;
            previous_admin = admin_reply;
            command_edges += cmd_valid && cmd_ready;
            tx_edges += tx_valid && tx_ready;
            stalls += held_tx || held_decision || held_admin;
            fenced += dut.admin_fenced;
        end
    end

    task automatic submit_source(
        input logic [KEY_W-1:0] slot, input logic [EPOCH_W-1:0] epoch,
        input logic [159:0] request_value, input logic resend
    );
        @(negedge clk);
        source_slot = slot;
        source_epoch = epoch;
        source_request = request_value;
        source_resend = resend;
        source_valid = 1;
        do @(posedge clk); while (!source_ready);
        @(negedge clk);
        source_valid = 0;
    endtask

    task automatic submit_admin(
        input logic [1:0] op, input logic [KEY_W-1:0] slot, input logic [EPOCH_W-1:0] epoch
    );
        @(negedge clk);
        admin_op = op;
        admin_epoch = epoch;
        admin_slot = slot;
        admin_valid = 1;
        do @(posedge clk); while (!admin_ready);
        @(negedge clk);
        admin_valid = 0;
    endtask

    task automatic settle_command(
        input logic [1:0] op, input logic [KEY_W-1:0] slot, input logic [EPOCH_W-1:0] epoch,
        input logic [159:0] request_value,
        input logic [3:0] status, input logic [TOKEN_W-1:0] token, input logic corrupt
    );
        while (!cmd_valid)
            @(negedge clk);
        repeat (3) begin
            if ({cmd_op,cmd_slot,cmd_epoch,cmd_request} !== {op,slot,epoch,request_value})
                $fatal(1,"forwarded command differs from the selected input");
            if (!cmd_enable || (op == 1 && cmd_ttl != 500))
                $fatal(1,"command lost its enable or deadline");
            @(negedge clk);
        end
        cmd_ready = 1;
        @(negedge clk);
        cmd_ready = 0;
        rsp_op = op;
        rsp_slot = slot;
        rsp_status = status;
        rsp_epoch = epoch;
        rsp_token = token;
        rsp_state = op == 1 ? 3'd1 : 3'd0;
        rsp_request = corrupt ? request_value ^ 160'd1 : request_value;
        rsp_ttl = op == 1 ? 32'd440 : 32'd0;
        rsp_valid = 1;
        do @(posedge clk); while (!rsp_ready);
        @(negedge clk);
        rsp_valid = 0;
    endtask

    task automatic take_decision(
        input logic [KEY_W-1:0] slot, input logic [EPOCH_W-1:0] epoch,
        input logic [TOKEN_W-1:0] token, input logic [3:0] status,
        input logic [1:0] action, input logic [31:0] ttl
    );
        while (!decision_valid)
            @(negedge clk);
        repeat (2) @(negedge clk);
        if (decision_word !== {slot,epoch,token,status,action,ttl})
            $fatal(1,"source disposition mismatch");
        decision_ready = 1;
        @(negedge clk);
        decision_ready = 0;
    endtask

    task automatic take_transport(
        input logic retry, input logic [KEY_W-1:0] slot, input logic [EPOCH_W-1:0] epoch,
        input logic [TOKEN_W-1:0] token, input logic [159:0] request_value
    );
        while (!tx_valid)
            @(negedge clk);
        repeat (2) @(negedge clk);
        if (tx_word !== {retry,slot,epoch,token,request_value})
            $fatal(1,"transport metadata or wire request differs");
        tx_ready = 1;
        @(negedge clk);
        tx_ready = 0;
    endtask

    task automatic take_admin_reply(input logic [1:0] op, input logic [KEY_W-1:0] slot,
                                   input logic [EPOCH_W-1:0] epoch);
        while (!admin_reply_valid)
            @(negedge clk);
        if (admin_reply !== {op,4'd0,slot,epoch,TOKEN_W'(0),1'b1,3'd0,
                             SESSION,80'd0,WINDOW'(0),32'd0})
            $fatal(1,"admin response lost snapshot fields");
        admin_reply_ready = 1;
        @(negedge clk);
        admin_reply_ready = 0;
    endtask

    initial begin
        repeat (3) @(negedge clk);
        rst = 0;
        submit_source(1,2,REQUEST,0);
        settle_command(1,1,2,REQUEST,0,1,0);
        submit_admin(0,1,3);
        submit_source(1,2,OTHER,0);
        repeat (12) begin
            @(negedge clk);
            if (cmd_valid)
                $fatal(1,"same-slot reconfiguration passed a held transport request");
        end
        take_transport(0,1,2,1,REQUEST);
        settle_command(0,1,3,{SESSION,80'd0},0,0,0);
        take_decision(1,2,1,0,1,440);
        // A queued source request can progress while the admin response stalls.
        settle_command(1,1,2,OTHER,2,0,0);
        take_decision(1,2,0,2,0,440);
        take_admin_reply(0,1,3);

        submit_source(1,3,REQUEST,0);
        settle_command(1,1,3,REQUEST,6,2,0);
        take_decision(1,3,2,6,3,440);
        if (tx_valid)
            $fatal(1,"disabled retransmission emitted a request");
        submit_source(1,3,REQUEST,1);
        settle_command(1,1,3,REQUEST,6,2,0);
        take_decision(1,3,2,6,2,440);
        take_transport(1,1,3,2,REQUEST);

        submit_source(2,4,OTHER,1);
        settle_command(1,2,4,OTHER,0,3,1);
        take_decision(2,4,0,15,0,0);
        if (tx_valid)
            $fatal(1,"a corrupted guard reply authorized transport");

        submit_admin(3,0,0);
        settle_command(3,0,0,{SESSION,80'd0},0,0,0);
        submit_source(1,3,OTHER,0);
        settle_command(1,1,3,OTHER,0,3,0);
        if (!tx_valid || !decision_valid || !admin_reply_valid ||
            {issued_requests,repeated_requests,suppressed_requests,rejected_requests,bad_responses} !==
            {32'd2,32'd1,32'd1,32'd2,32'd1})
            $fatal(1,"independent output slots or lifetime statistics disagree");
        rst = 1;
        #1;
        if (tx_valid || decision_valid || admin_reply_valid || cmd_valid || rsp_ready)
            $fatal(1,"a dispatcher transfer remained possible during reset");
        @(negedge clk);
        if (issued_requests || repeated_requests || suppressed_requests || rejected_requests || bad_responses)
            $fatal(1,"reset did not clear dispatcher statistics");
        $display("PASS recovery dispatch: %0d guard commands, %0d transport transfers, %0d fenced and %0d held cycles",
                 command_edges,tx_edges,fenced,stalls);
        $finish;
    end

    initial begin
        #20000;
        $fatal(1,"dispatcher regression timed out");
    end
endmodule

`default_nettype wire
