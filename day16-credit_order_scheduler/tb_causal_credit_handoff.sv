`timescale 1ns/1ps
`default_nettype none

module tb_causal_credit_handoff;
    localparam integer PAYLOAD_W=38;
    logic clk=0,rst=1;
    always #5 clk=!clk;

    logic g_cfg_valid=0;
    wire g_cfg_ready, g_cfg_ack;
    logic [1:0] g_cfg_session=0;
    wire [3:0] g_cfg_status;
    logic g_input=0, g_dep=0;
    wire g_input_ready;
    logic [1:0] g_session=0;
    logic [3:0] g_sequence=0;
    logic [15:0] g_tag=0;
    wire [3:0] g_input_status;
    wire g_valid,g_applicable;
    wire [1:0] g_out_session,g_out_epoch;
    wire [3:0] g_out_sequence;
    wire [7:0] g_cost;
    wire [15:0] g_out_tag;
    wire [31:0] g_out_body;
    logic g_ack=0;
    logic [1:0] g_ack_session=0;
    logic [3:0] g_ack_seq=0;
    wire g_terminal;
    wire [2:0] g_outcome;
    wire [15:0] g_done_tag;

    wire enqueue_ready;
    wire gate_ready=enqueue_ready || !g_applicable;
    causal_order_gate #(.SESSIONS(2),.WINDOW(3),.EPOCH_W(2),.SEQ_W(4),.TTL_W(8)) gate (
        .clk,.rst,.c_valid(g_cfg_valid),.c_ready(g_cfg_ready),.c_op(2'd0),.c_session(g_cfg_session),
        .c_expected_epoch(2'd0),.c_sequence(4'd1),.reply_valid(g_cfg_ack),.reply_ready(1'b1),
        .reply_status(g_cfg_status),.reply_session(),.reply_epoch(),.reply_next_sequence(),
        .reply_row_state(),.reply_armed(),.reply_exhausted(),
        .s_valid(g_input),.s_ready(g_input_ready),.s_session(g_session),.s_epoch(2'd1),
        .s_sequence(g_sequence),.s_dependent(g_dep),.s_predecessor(4'd1),.s_lifetime(8'd240),
        .s_cost(8'd3),.s_tag(g_tag),.s_payload({16'habcd,g_tag}),.s_status(g_input_status),
        .m_valid(g_valid),.m_ready(gate_ready),.m_applicable(g_applicable),
        .m_session(g_out_session),.m_epoch(g_out_epoch),.m_sequence(g_out_sequence),
        .m_cost(g_cost),.m_tag(g_out_tag),.m_payload(g_out_body),
        .ack_valid(g_ack),.ack_ready(),.ack_session(g_ack_session),.ack_epoch(2'd1),
        .ack_sequence(g_ack_seq),.ack_success(1'b1),
        .d_valid(g_terminal),.d_ready(1'b1),.d_session(),.d_epoch(),.d_sequence(),
        .d_tag(g_done_tag),.d_status(g_outcome),.stale_acks(),.duplicate_acks(),.conflicting_acks()
    );

    wire tx_valid,tx_lane,credit_ack;
    logic tx_ready=0,credit_valid=0,credit_lane=0;
    wire credit_ready,credit_input_rejected;
    logic [1:0] credit_op=0;
    logic [5:0] credit_token=0;
    wire [5:0] tx_token;
    wire [7:0] tx_cost, available, in_flight;
    wire [15:0] tx_tag;
    wire [PAYLOAD_W-1:0] tx_record;
    wire [2:0] credit_result;

    leased_credit_scheduler #(
        .CLASSES(2),.DEPTH(2),.PAYLOAD_W(PAYLOAD_W),.LEASE_SLOTS(3),.GEN_W(4)
    ) transport (
        .clk,.rst,
        .s_valid(g_valid && g_applicable && enqueue_ready),.s_ready(enqueue_ready),
        .s_class(g_out_session[0]),.s_cost(g_cost),.s_tag(g_out_tag),
        .s_payload({g_out_epoch,g_out_sequence,g_out_body}),.s_rejected(credit_input_rejected),
        .m_valid(tx_valid),.m_ready(tx_ready),.m_class(tx_lane),.m_cost(tx_cost),
        .m_tag(tx_tag),.m_payload(tx_record),.m_lease(tx_token),
        .ctl_valid(credit_valid),.ctl_ready(credit_ready),.ctl_op(credit_op),
        .ctl_class(credit_lane),.ctl_amount(8'd3),.ctl_weight(4'd1),
        .ctl_lease(credit_token),.ctl_tag(16'd0),
        .report_valid(credit_ack),.report_ready(1'b1),.report_op(),.report_class(),
        .report_status(credit_result),.report_capacity(),.report_available(available),
        .report_inflight(in_flight),.report_queued(),.report_weight(),.report_lease(),
        .report_tag(),.lease_active(),.lease_exhausted()
    );

    integer sent=0, results=0;
    always @(posedge clk) begin
        if(!rst) begin
            if(credit_input_rejected) $fatal(1,"transport rejected a valid ordered action");
            sent <= sent+(tx_valid && tx_ready);
            if(g_terminal) begin
                if(g_outcome!=0 || (g_done_tag!=16'h11 && g_done_tag!=16'h12 && g_done_tag!=16'h21))
                    $fatal(1,"unexpected terminal result");
                results<=results+1;
            end
        end
    end

    task automatic configure_credit(input logic [1:0] op,input logic lane,input logic [5:0] token);
        @(negedge clk);
        {credit_valid,credit_op,credit_lane,credit_token}={1'b1,op,lane,token};
        @(posedge clk);
        while(!credit_ready) @(posedge clk);
        @(negedge clk);
        credit_valid=0;
        if(!credit_ack || credit_result!=0 || available!=3 || in_flight!=0)
            $fatal(1,"credit command failed");
        @(negedge clk);
    endtask

    task automatic open_session(input logic [1:0] lane);
        @(negedge clk);
        {g_cfg_valid,g_cfg_session}={1'b1,lane};
        @(posedge clk);
        if(!g_cfg_ready) $fatal(1,"could not open session");
        @(negedge clk);
        g_cfg_valid=0;
        if(!g_cfg_ack || g_cfg_status!=0) $fatal(1,"session was not configured");
        @(negedge clk);
    endtask

    task automatic order(input logic [1:0] lane,input logic [3:0] seq,
                         input logic dep,input logic [15:0] tag);
        @(negedge clk);
        {g_input,g_session,g_sequence,g_dep,g_tag}={1'b1,lane,seq,dep,tag};
        @(posedge clk);
        if(!g_input_ready || g_input_status!=0) $fatal(1,"order was not accepted");
        @(negedge clk);
        g_input=0;
    endtask

    task automatic transmit(input logic lane,input logic [3:0] seq,
                            input logic [15:0] tag,output logic [5:0] token);
        while(!tx_valid) @(negedge clk);
        repeat(3) begin
            if({tx_lane,tx_cost,tx_tag,tx_record} !== {lane,8'd3,tag,2'd1,seq,16'habcd,tag})
                $fatal(1,"transport lost the original causal identity");
            @(negedge clk);
        end
        token=tx_token;
        tx_ready=1;
        @(negedge clk);
        tx_ready=0;
    endtask

    task automatic confirm(input logic [1:0] lane,input logic [3:0] seq);
        @(negedge clk);
        {g_ack,g_ack_session,g_ack_seq}={1'b1,lane,seq};
        @(negedge clk);
        g_ack=0;
    endtask

    initial begin
        logic [5:0] a,x,b;
        repeat(2) @(negedge clk);
        rst=0;
        configure_credit(0,0,0);
        configure_credit(0,1,0);
        open_session(0);
        open_session(1);
        order(0,1,0,'h11);
        order(0,2,1,'h12);
        order(1,1,0,'h21);
        transmit(0,1,'h11,a);
        configure_credit(1,0,a);
        transmit(1,1,'h21,x);
        repeat(8) begin
            @(negedge clk);
            if(tx_valid) $fatal(1,"a returned transport lease was treated as venue success");
        end
        confirm(0,1);
        transmit(0,2,'h12,b);
        if(b==a || b==x) $fatal(1,"transport reused a still-identifiable lease");
        configure_credit(1,1,x);
        configure_credit(1,0,b);
        confirm(1,1);
        confirm(0,2);
        repeat(8) @(negedge clk);
        if(sent!=3 || results!=3 || tx_valid) $fatal(1,"credit and causal flow did not retire");
        $display("PASS causal credit handoff: independent lease refunds and venue acknowledgment");
        $finish;
    end

    initial begin
        #20000;
        $fatal(1,"causal credit integration timeout");
    end
endmodule

`default_nettype wire
