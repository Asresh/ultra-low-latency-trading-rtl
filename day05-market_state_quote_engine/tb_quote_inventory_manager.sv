`timescale 1ns/1ps
`default_nettype none

module tb_quote_inventory_manager #(
    parameter integer INSTRUMENTS=3,
    parameter integer LEVELS=31
);
    localparam integer POS_W=8,QTY_W=6,RESERVE_W=9,EPOCH_W=3,SEQ_W=4,REV_W=3,TAG_W=10;
    localparam integer PRICE_W=$clog2(LEVELS+1),INST_W=$clog2(INSTRUMENTS+1);
    localparam integer CONTROL_W=4+INST_W+TAG_W+REV_W+SEQ_W+3*QTY_W+4*POS_W+PRICE_W;
    localparam integer REPLY_W=8+INST_W+TAG_W+REV_W+SEQ_W+POS_W+64;
    localparam integer QUOTE_W=3+INST_W+EPOCH_W+2*PRICE_W+QTY_W;
    localparam integer OFFER_W=5+INST_W+EPOCH_W+REV_W+2*PRICE_W+2*QTY_W+POS_W;
    logic clk=0,rst=1;
    always #5 clk=!clk;

    logic c_valid=0,r_ready=0,c_sell;
    wire c_ready,r_valid,r_armed;
    logic [1:0] c_op;
    logic [INST_W-1:0] c_instrument;
    logic [TAG_W-1:0] c_tag;
    logic [REV_W-1:0] c_expected_revision;
    logic [SEQ_W-1:0] c_sequence;
    logic [QTY_W-1:0] c_quantity,c_bid_cap,c_ask_cap;
    logic signed [POS_W-1:0] c_position,c_floor,c_ceiling;
    logic [POS_W-1:0] c_skew_trigger;
    logic [PRICE_W-1:0] c_skew_ticks;

    wire [1:0] r_op;
    wire [INST_W-1:0] r_instrument;
    wire [TAG_W-1:0] r_tag;
    wire [3:0] r_status;
    wire [REV_W-1:0] r_revision;
    wire [SEQ_W-1:0] r_next_sequence;
    wire signed [POS_W-1:0] r_position;
    wire [31:0] r_fills,r_faults;

    logic s_valid=0,s_active,s_current,m_ready=0;
    wire s_ready,m_valid,m_current;
    logic [INST_W-1:0] s_instrument;
    logic [EPOCH_W-1:0] s_epoch;
    logic [PRICE_W-1:0] s_bid,s_ask;
    logic [QTY_W-1:0] s_size;
    logic [INSTRUMENTS-1:0] market_eligible;
    logic [INSTRUMENTS*EPOCH_W-1:0] market_epoch;
    logic [INSTRUMENTS*RESERVE_W-1:0] bid_reserved,ask_reserved;
    wire [INST_W-1:0] m_instrument;
    wire [EPOCH_W-1:0] m_epoch;
    wire [REV_W-1:0] m_revision;
    wire [PRICE_W-1:0] m_bid,m_ask;
    wire [QTY_W-1:0] m_bid_size,m_ask_size;
    wire signed [POS_W-1:0] m_position;
    wire [2:0] m_reason;
    wire [INSTRUMENTS-1:0] bid_eligible,ask_eligible;
    wire [INSTRUMENTS*QTY_W-1:0] live_bid_size,live_ask_size;

    quote_inventory_manager #(
        .INSTRUMENTS(INSTRUMENTS),.LEVELS(LEVELS),.POS_W(POS_W),.QTY_W(QTY_W),
        .RESERVE_W(RESERVE_W),.EPOCH_W(EPOCH_W),.SEQ_W(SEQ_W),.REV_W(REV_W),.TAG_W(TAG_W)
    ) dut (.*);

    wire [REPLY_W-1:0] actual_reply = !r_valid ? '0 :
        {r_valid,r_op,r_instrument,r_tag,r_status,r_armed,r_revision,r_next_sequence,
         r_position,r_fills,r_faults};
    wire [OFFER_W-1:0] actual_offer = !m_valid ? '0 :
        {m_valid,m_current,m_instrument,m_epoch,m_revision,m_bid,m_ask,m_bid_size,m_ask_size,
         m_position,m_reason};
    logic [CONTROL_W-1:0] control;
    logic [QUOTE_W-1:0] market;
    logic [REPLY_W-1:0] expected_reply;
    logic [OFFER_W-1:0] expected_offer;
    logic expected_c_ready,expected_s_ready;
    logic [INSTRUMENTS*QTY_W-1:0] expected_bid,expected_ask;
    logic held=0,reply_held=0;
    logic [OFFER_W-3:0] held_offer='0;
    logic [REPLY_W-1:0] held_reply='0;
    integer cycle=0, cmds=0, published=0, ignored=0, stalls=0;
    string filename,wave;

    initial begin
        integer source,rc;
        if (!$value$plusargs("vectors=%s",filename)) $fatal(1,"missing model vectors");
        if ($value$plusargs("wave=%s",wave)) begin
            $dumpfile(wave);
            $dumpvars(0,tb_quote_inventory_manager);
        end
        source=$fopen(filename,"r");
        if (!source) $fatal(1,"cannot open model vectors");
        @(negedge clk);
        rc = $fscanf(source,"%h %h %h %h %h %h %h %h %h %h %h %h %h %h %h\n",
                     rst,r_ready,m_ready,control,market,market_eligible,market_epoch,
                     bid_reserved,ask_reserved,expected_c_ready,expected_reply,expected_s_ready,
                     expected_offer,expected_bid,expected_ask);
        while (rc==15) begin
            {c_valid,c_op,c_instrument,c_tag,c_expected_revision,c_sequence,c_sell,c_quantity,
             c_position,c_floor,c_ceiling,c_bid_cap,c_ask_cap,c_skew_trigger,c_skew_ticks}=control;
            {s_valid,s_instrument,s_active,s_current,s_epoch,s_bid,s_ask,s_size}=market;
            #1;
            if ({c_ready,s_ready,live_bid_size,live_ask_size} !==
                {expected_c_ready,expected_s_ready,expected_bid,expected_ask})
                $fatal(1,"inventory readiness or bounds mismatch at cycle %0d",cycle);
            for (integer bank=0;bank<INSTRUMENTS;bank=bank+1) begin
                if (bid_eligible[bank] !== (expected_bid[QTY_W*bank+:QTY_W] != 0) ||
                    ask_eligible[bank] !== (expected_ask[QTY_W*bank+:QTY_W] != 0))
                    $fatal(1,"side permission disagrees with live size at cycle %0d",cycle);
            end
            if (actual_reply !== expected_reply)
                $fatal(1,"reply at cycle %0d got=%h expect=%h",cycle,actual_reply,expected_reply);
            if (actual_offer !== expected_offer)
                $fatal(1,"offer at cycle %0d got=%h expect=%h",cycle,actual_offer,expected_offer);
            if (!rst && held && (!m_valid || held_offer !== actual_offer[OFFER_W-3:0]))
                $fatal(1,"held quote fields changed at cycle %0d",cycle);
            if (!rst && reply_held && held_reply !== actual_reply)
                $fatal(1,"held control response changed at cycle %0d",cycle);
            held=m_valid && !m_ready;
            held_offer=actual_offer[OFFER_W-3:0];
            reply_held=r_valid && !r_ready;
            held_reply=actual_reply;
            cmds=cmds+(c_valid && c_ready);
            published=published+(m_valid && m_ready && m_current);
            ignored=ignored+(m_valid && m_ready && !m_current);
            stalls=stalls+(m_valid && !m_ready);
            cycle=cycle+1;
            @(negedge clk);
            rc = $fscanf(source,"%h %h %h %h %h %h %h %h %h %h %h %h %h %h %h\n",
                         rst,r_ready,m_ready,control,market,market_eligible,market_epoch,
                         bid_reserved,ask_reserved,expected_c_ready,expected_reply,expected_s_ready,
                         expected_offer,expected_bid,expected_ask);
        end
        if (rc != -1) $fatal(1,"bad oracle row");
        if (cmds < 10 || published < 4 || ignored < 1 || stalls < 8)
            $fatal(1,"insufficient inventory fixture coverage");
        $display("PASS inventory manager banks=%0d levels=%0d cycles=%0d commands=%0d current=%0d waived=%0d held=%0d",
                 INSTRUMENTS,LEVELS,cycle,cmds,published,ignored,stalls);
        $finish;
    end

    initial begin
        #2000000;
        $fatal(1,"inventory manager scoreboard timeout");
    end
endmodule

`default_nettype wire
