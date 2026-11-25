`timescale 1ns/1ps
`default_nettype none

module tb_causal_order_gate;
    logic clk=0,rst=1;
    always #5 clk=!clk;
    localparam integer ACTION_W=2+2+4+8+16+32;
    localparam integer DONE_W=2+2+4+16+3;
    logic c_valid=0,reply_ready=0;
    wire c_ready,reply_valid,reply_armed,reply_exhausted;
    logic [1:0] c_op=0,c_session=0,c_expected_epoch=0;
    logic [3:0] c_sequence=0;
    wire [3:0] reply_status,reply_next_sequence;
    wire [1:0] reply_session,reply_epoch,reply_row_state;

    logic s_valid=0,s_dependent=0;
    wire s_ready;
    logic [1:0] s_session=0,s_epoch=0;
    logic [3:0] s_sequence=0,s_predecessor=0;
    logic [7:0] s_lifetime=0,s_cost=0;
    logic [15:0] s_tag=0;
    logic [31:0] s_payload=0;
    wire [3:0] s_status;

    wire m_valid,m_applicable;
    logic m_ready=0;
    wire [1:0] m_session,m_epoch;
    wire [3:0] m_sequence;
    wire [7:0] m_cost;
    wire [15:0] m_tag;
    wire [31:0] m_payload;

    logic ack_valid=0,ack_success=0;
    wire ack_ready;
    logic [1:0] ack_session=0,ack_epoch=0;
    logic [3:0] ack_sequence=0;

    wire d_valid;
    logic d_ready=0;
    wire [1:0] d_session,d_epoch;
    wire [3:0] d_sequence;
    wire [15:0] d_tag;
    wire [2:0] d_status;
    wire [31:0] stale_acks,duplicate_acks,conflicting_acks;
    causal_order_gate #(.SESSIONS(2),.WINDOW(3),.EPOCH_W(2),.SEQ_W(4),.TTL_W(8)) dut (.*);

    wire [ACTION_W-1:0] action={m_session,m_epoch,m_sequence,m_cost,m_tag,m_payload};
    wire [DONE_W-1:0] verdict={d_session,d_epoch,d_sequence,d_tag,d_status};
    logic action_held=0,verdict_held=0;
    logic [ACTION_W-1:0] past_action;
    logic [DONE_W-1:0] past_verdict;
    integer submitted=0,waived=0,retired=0,cycle=0,capture=0;

    always @(posedge clk) begin
        if (rst) begin
            action_held<=0;
            verdict_held<=0;
        end else begin
            if(action_held && (!m_valid || action!==past_action))
                $fatal(1,"held causal action mutated");
            if(verdict_held && (!d_valid || verdict!==past_verdict))
                $fatal(1,"held causal verdict mutated");
            cycle<=cycle+1;
            if(capture) begin
                if(s_valid && s_ready && s_status==0)
                    $fdisplay(capture,"I %0d %0d %0d %0d %0d %0d %0d",cycle,s_session,s_epoch,
                              s_sequence,s_tag,s_dependent,s_predecessor);
                if(m_valid && m_ready)
                    $fdisplay(capture,"T %0d %0d %0d %0d %0d %0d",cycle,m_session,m_epoch,
                              m_sequence,m_tag,m_applicable);
                if(ack_valid && ack_ready)
                    $fdisplay(capture,"A %0d %0d %0d %0d %0d",cycle,ack_session,ack_epoch,
                              ack_sequence,ack_success);
                if(d_valid && d_ready)
                    $fdisplay(capture,"D %0d %0d %0d %0d %0d %0d",cycle,d_session,d_epoch,
                              d_sequence,d_tag,d_status);
            end
            action_held<=m_valid && !m_ready;
            verdict_held<=d_valid && !d_ready;
            past_action<=action;
            past_verdict<=verdict;
            submitted<=submitted+(m_valid && m_ready && m_applicable);
            waived<=waived+(m_valid && m_ready && !m_applicable);
            retired<=retired+(d_valid && d_ready);
        end
    end

    function automatic logic [15:0] tag_for(input logic [1:0] venue,
                                           input logic [1:0] epoch,input logic [3:0] seq);
        tag_for={8'hb4,venue,epoch,seq};
    endfunction

    task automatic prepare_source(input logic [1:0] venue,epoch,input logic [3:0] seq,prior,
                                  input logic dep,input logic [7:0] life);
        {s_session,s_epoch,s_sequence,s_dependent,s_predecessor,s_lifetime,s_cost}=
            {venue,epoch,seq,dep,prior,life,8'd3};
        s_tag=tag_for(venue,epoch,seq);
        s_payload={16'hacce,s_tag};
    endtask

    task automatic source(input logic [1:0] venue,epoch,input logic [3:0] seq,prior,
                          input logic dep,input logic [7:0] age,input logic [3:0] status);
        @(negedge clk);
        prepare_source(venue,epoch,seq,prior,dep,age);
        s_valid=1;
        @(posedge clk);
        if(!s_ready || s_status!==status)
            $fatal(1,"bad admission venue=%0d seq=%0d status=%0d expected=%0d ready=%b",
                   venue,seq,s_status,status,s_ready);
        @(negedge clk);
        s_valid=0;
    endtask

    task automatic consume_reply(input logic [1:0] venue,epoch,input logic [3:0] status,
                                 input logic armed_status);
        while(!reply_valid) @(negedge clk);
        repeat(2) begin
            if(!reply_valid || {reply_session,reply_epoch,reply_status,reply_armed} !==
                                {venue,epoch,status,armed_status})
                $fatal(1,"control failed time=%0t session=%0d status=%0d expected=%0d",
                       $time,reply_session,reply_status,status);
            @(negedge clk);
        end
        reply_ready=1;
        @(negedge clk);
        reply_ready=0;
    endtask

    task automatic control(input logic [1:0] op,venue,prior_epoch,result_epoch,
                           input logic [3:0] seq,status,input logic armed_status);
        @(negedge clk);
        {c_valid,c_op,c_session,c_expected_epoch,c_sequence}={1'b1,op,venue,prior_epoch,seq};
        @(posedge clk);
        if(!c_ready) $fatal(1,"control not accepted");
        @(negedge clk);
        c_valid=0;
        consume_reply(venue,result_epoch,status,armed_status);
    endtask

    task automatic expect_action(input logic [1:0] venue,epoch,input logic [3:0] seq,input logic allowed);
        while(!m_valid) @(negedge clk);
        #1;
        if (action !== {venue,epoch,seq,8'd3,tag_for(venue,epoch,seq),16'hacce,tag_for(venue,epoch,seq)}
            || m_applicable!==allowed)
            $fatal(1,"wrong action at %0t venue=%0d epoch=%0d seq=%0d applicable=%b",
                   $time,m_session,m_epoch,m_sequence,m_applicable);
    endtask

    task automatic transmit;
        @(negedge clk);
        m_ready=1;
        @(negedge clk);
        m_ready=0;
    endtask

    task automatic acknowledge(input logic [1:0] venue,epoch,input logic [3:0] seq,input logic success);
        @(negedge clk);
        {ack_valid,ack_session,ack_epoch,ack_sequence,ack_success}={1'b1,venue,epoch,seq,success};
        @(posedge clk);
        if(!ack_ready) $fatal(1,"venue evidence backpressured");
        @(negedge clk);
        ack_valid=0;
    endtask

    task automatic outcome(input logic [1:0] venue,epoch,input logic [3:0] seq,input logic [2:0] status);
        while(!d_valid) @(negedge clk);
        repeat(3) begin
            if(!d_valid || verdict !== {venue,epoch,seq,tag_for(venue,epoch,seq),status})
                $fatal(1,"wrong outcome at %0t venue=%0d seq=%0d status=%0d",$time,d_session,d_sequence,d_status);
            @(negedge clk);
        end
        d_ready=1;
        @(negedge clk);
        d_ready=0;
    endtask

    initial begin
        string wave, capture_file;
        if($value$plusargs("capture=%s",capture_file)) begin
            capture=$fopen(capture_file,"w");
            if(!capture) $fatal(1,"unable to open causal gate capture");
        end
        if($value$plusargs("wave=%s",wave)) begin
            $dumpfile(wave);
            $dumpvars(0,tb_causal_order_gate);
        end
        repeat(2) @(negedge clk);
        rst=0;
        control(0,0,0,1,1,0,1);
        control(0,1,0,1,8,0,1);
        source(0,1,1,0,0,180,0);
        expect_action(0,1,1,1);
        source(0,1,2,1,1,180,0);
        source(0,1,3,2,1,180,0);
        prepare_source(0,1,4,0,0,180);
        #1;
        if(s_ready || s_status!=0) $fatal(1,"full window admitted a fourth row");
        source(0,1,4,4,1,180,6);
        source(1,1,8,0,0,180,0);
        transmit();
        expect_action(1,1,8,1);
        transmit();
        acknowledge(0,0,1,1);
        acknowledge(1,1,8,1);
        outcome(1,1,8,0);
        control(2,0,1,1,1,3,1);
        repeat(4) @(negedge clk);
        if(m_valid) $fatal(1,"child escaped before predecessor acknowledgment");
        acknowledge(0,1,1,1);
        outcome(0,1,1,0);
        expect_action(0,1,2,1);
        control(2,0,1,1,1,3,1);
        transmit();
        control(2,0,1,1,1,0,1);
        acknowledge(0,1,2,0);
        outcome(0,1,2,1);
        outcome(0,1,3,2);
        acknowledge(0,1,2,0);
        source(0,1,4,1,1,180,6);
        source(0,1,4,0,0,6,0);
        expect_action(0,1,4,1);
        repeat(8) @(negedge clk);
        expect_action(0,1,4,0);
        outcome(0,1,4,3);
        control(2,0,1,1,4,3,1);
        transmit();
        control(2,0,1,1,4,0,1);

        source(0,1,5,0,0,180,0);
        expect_action(0,1,5,1);
        @(negedge clk);
        {ack_valid,ack_session,ack_epoch,ack_sequence,ack_success}={1'b1,2'd0,2'd1,4'd2,1'b1};
        m_ready=1;
        #1;
        if(m_applicable) $fatal(1,"conflicting acknowledgment did not fence the transfer edge");
        @(negedge clk);
        {ack_valid,m_ready}=0;
        outcome(0,1,5,4);
        source(0,1,6,0,0,180,3);

        source(1,1,9,8,1,180,0);
        expect_action(1,1,9,1);
        transmit();
        acknowledge(1,1,9,1);
        outcome(1,1,9,0);
        @(negedge clk);
        {c_valid,c_op,c_session,c_expected_epoch,c_sequence}={1'b1,2'd2,2'd1,2'd1,4'd9};
        prepare_source(1,1,10,9,1,180);
        s_valid=1;
        @(posedge clk);
        if(!c_ready || !s_ready || s_status!=6)
            $fatal(1,"same-edge reclaim admitted a dangling dependency");
        @(negedge clk);
        {c_valid,s_valid}=0;
        consume_reply(1,1,0,1);
        source(1,1,10,0,0,180,0);
        expect_action(1,1,10,1);
        transmit();
        acknowledge(1,1,10,1);
        outcome(1,1,10,0);

        control(0,0,1,1,15,3,0);
        control(2,0,1,1,2,0,0);
        control(2,0,1,1,3,0,0);
        control(2,0,1,1,5,0,0);
        control(0,0,1,2,15,0,1);
        source(0,1,15,0,0,180,2);
        source(0,2,15,0,0,180,0);
        source(0,2,15,0,0,180,7);
        expect_action(0,2,15,1);
        transmit();
        acknowledge(0,2,15,1);
        outcome(0,2,15,0);
        control(2,0,2,2,15,0,1);
        control(0,0,2,3,1,0,1);
        control(0,0,3,3,1,4,1);
        if({stale_acks,duplicate_acks,conflicting_acks} !== {32'd1,32'd1,32'd1})
            $fatal(1,"acknowledgment classifications differ");
        if(submitted!=6 || waived!=2 || retired!=9)
            $fatal(1,"terminal conservation differs: sends=%0d waived=%0d outcomes=%0d",submitted,waived,retired);
        $display("PASS causal gate sends=%0d waived=%0d terminal=%0d",submitted,waived,retired);
        if(capture) $fclose(capture);
        $finish;
    end

    initial begin
        #40000;
        $fatal(1,"causal gate test timed out");
    end
endmodule

`default_nettype wire
