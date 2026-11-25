`timescale 1ns/1ps
`default_nettype none

module tb_fx_cash_converter #(
    parameter integer CURRENCIES=5,ROWS=7,RATE_W=32,RATE_FRAC=24
);
    localparam integer TIME_W=16,EPOCH_W=3;
    localparam integer CCY_W=$clog2(CURRENCIES+1),ROW_W=$clog2(ROWS+1);
    localparam integer CFG_W=2+ROW_W+2*CCY_W+2*RATE_W+TIME_W;
    localparam integer REQUEST_W=85+3*CCY_W+TIME_W;
    localparam integer RESULT_W=88+2*ROW_W+TIME_W+EPOCH_W;
    logic clk=0,rst=1;
    always #5 clk=!clk;
    logic [TIME_W-1:0] tick;
    logic cfg_valid=0,cfg_enable,commit_valid=0;
    wire cfg_ready,cfg_error,commit_ready;
    logic [ROW_W-1:0] cfg_row;
    logic [CCY_W-1:0] cfg_from,cfg_to;
    logic [RATE_W-1:0] cfg_bid,cfg_ask;
    logic [TIME_W-1:0] cfg_observed_tick;
    wire [EPOCH_W-1:0] active_epoch;

    logic s_valid=0,s_allow_pivot,m_ready=0;
    wire s_ready,m_valid,m_inexact;
    logic [15:0] s_tag;
    logic [CCY_W-1:0] s_from,s_to,s_pivot;
    logic [TIME_W-1:0] s_max_age;
    logic signed [64:0] s_amount;
    logic [1:0] s_round;

    wire [15:0] m_tag;
    wire [2:0] m_status;
    wire signed [64:0] m_amount;
    wire [1:0] m_hops;
    wire [ROW_W-1:0] m_first_row,m_second_row;
    wire [TIME_W-1:0] m_oldest_age;
    wire [EPOCH_W-1:0] m_epoch;
    fx_cash_converter #(.CURRENCIES(CURRENCIES),.ROWS(ROWS),.RATE_W(RATE_W),.RATE_FRAC(RATE_FRAC),
                        .TIME_W(TIME_W),.EPOCH_W(EPOCH_W)) dut (.*);

    wire [RESULT_W-1:0] result=m_valid ?
        {m_valid,m_tag,m_status,m_amount,m_hops,m_first_row,m_second_row,m_oldest_age,m_epoch,m_inexact} : '0;
    logic [CFG_W-1:0] cfg_word;
    logic [REQUEST_W-1:0] request_word;
    logic [RESULT_W-1:0] expected_result;
    logic expected_cfg_ready,expected_cfg_error,expected_s_ready;
    logic [EPOCH_W-1:0] expected_epoch;
    logic held=0,cfg_held=0;
    logic [CFG_W-1:0] held_cfg_word;
    logic [RESULT_W-1:0] held_result;
    integer cycles=0,answers=0,positive=0,negative=0,two=0,hold_count=0;
    integer statuses[0:4];
    integer config_stalls=0;
    string source,wave;

    initial begin
        integer handle,rc;
        if(!$value$plusargs("vectors=%s",source)) $fatal(1,"missing FX reference tape");
        if($value$plusargs("wave=%s",wave)) begin
            $dumpfile(wave);
            $dumpvars(0,tb_fx_cash_converter);
        end
        handle=$fopen(source,"r");
        if(!handle) $fatal(1,"FX compare tape unavailable");
        for(integer n=0;n<5;n=n+1) statuses[n]=0;
        repeat(3) @(negedge clk);
        rst=0;
        rc=$fscanf(handle,"%h %h %h %h %h %h %h %h %h %h\n",tick,m_ready,commit_valid,
                   cfg_word,request_word,expected_cfg_ready,expected_cfg_error,expected_epoch,
                   expected_s_ready,expected_result);
        while(rc==10) begin
            {cfg_valid,cfg_row,cfg_enable,cfg_from,cfg_to,cfg_bid,cfg_ask,cfg_observed_tick}=cfg_word;
            {s_valid,s_tag,s_from,s_to,s_allow_pivot,s_pivot,s_max_age,s_amount,s_round}=request_word;
            #1;
            if({cfg_ready,cfg_error,active_epoch,s_ready,commit_ready} !==
                {expected_cfg_ready,expected_cfg_error,expected_epoch,expected_s_ready,1'b1})
                $fatal(1,"FX control compare failed cycle=%0d",cycles);
            if(result!==expected_result)
                $fatal(1,"FX result at cycle=%0d got=%h expected=%h",cycles,result,expected_result);
            if(held && result!==held_result) $fatal(1,"held FX conversion changed");
            if(cfg_held && cfg_word!==held_cfg_word) $fatal(1,"FX source changed a held config");
            cfg_held=cfg_valid && !cfg_ready;
            held_cfg_word=cfg_word;
            config_stalls=config_stalls+cfg_held;
            held=m_valid && !m_ready;
            held_result=result;
            hold_count=hold_count+held;
            if(m_valid && m_ready) begin
                answers=answers+1;
                statuses[m_status]=statuses[m_status]+1;
                positive=positive+int'(m_amount>0);
                negative=negative+int'(m_amount<0);
                two=two+int'(m_hops==2);
            end
            cycles=cycles+1;
            @(negedge clk);
            rc=$fscanf(handle,"%h %h %h %h %h %h %h %h %h %h\n",tick,m_ready,commit_valid,
                   cfg_word,request_word,expected_cfg_ready,expected_cfg_error,expected_epoch,
                   expected_s_ready,expected_result);
        end
        if(rc!=-1 || !positive || !negative || !two || !hold_count || !config_stalls)
            $fatal(1,"missing FX scenario coverage");
        for(integer n=0;n<5;n=n+1)
            if(!statuses[n]) $fatal(1,"missing FX status %0d",n);
        $display("PASS FX cash ccys=%0d rows=%0d q=%0d cycles=%0d replies=%0d positive=%0d negative=%0d hops2=%0d held=%0d",
                 CURRENCIES,ROWS,RATE_FRAC,cycles,answers,positive,negative,two,hold_count);
        $finish;
    end

    initial begin
        #2000000;
        $fatal(1,"FX reference compare timed out");
    end
endmodule

`default_nettype wire
