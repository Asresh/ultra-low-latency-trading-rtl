`timescale 1ns/1ps
`default_nettype none

module tb_tiered_fee_settlement #(
    parameter integer VENUES=3, TIERS=4, PRICE_FRAC=8
);
    localparam integer REV_W=3,SEQ_W=5,VENUE_W=$clog2(VENUES+1);
    localparam integer BREAKS=TIERS>1 ? TIERS-1 : 1;
    localparam integer TIER_W=$clog2(TIERS+1);
    localparam integer CONTROL_W=69+VENUE_W+REV_W+SEQ_W;
    localparam integer REPLY_W=279+VENUE_W+REV_W+SEQ_W+TIER_W;

    logic clk=0,rst=1;
    always #5 clk=!clk;
    logic c_valid=0,c_rsp_ready=0;
    wire c_ready,c_rsp_valid,c_rsp_armed;
    logic [1:0] c_op;
    logic [VENUE_W-1:0] c_venue;
    logic [REV_W-1:0] c_expected_revision;
    logic [SEQ_W-1:0] c_next_sequence;
    logic [63:0] c_turnover;
    logic [64*BREAKS-1:0] c_thresholds;
    logic [16*TIERS-1:0] c_maker_rates,c_taker_rates;
    wire [3:0] c_rsp_status;
    wire [VENUE_W-1:0] c_rsp_venue;
    wire [REV_W-1:0] c_rsp_revision;
    wire [SEQ_W-1:0] c_rsp_sequence;
    wire [63:0] c_rsp_turnover;

    logic s_valid=0,m_ready=0,s_maker,s_sell;
    wire s_ready,m_valid,m_inexact;
    logic [VENUE_W-1:0] s_venue;
    logic [15:0] s_tag;
    logic [REV_W-1:0] s_revision;
    logic [SEQ_W-1:0] s_sequence;
    logic [31:0] s_price,s_quantity;
    logic [1:0] s_round;
    wire [VENUE_W-1:0] m_venue;
    wire [15:0] m_tag;
    wire [3:0] m_status;
    wire [REV_W-1:0] m_revision;
    wire [SEQ_W-1:0] m_sequence;
    wire [63:0] m_gross,m_turnover;
    wire signed [64:0] m_fee;
    wire [64:0] m_cash;
    wire [TIER_W-1:0] m_tiers;

    tiered_fee_settlement #(
        .VENUES(VENUES),.TIERS(TIERS),.PRICE_FRAC(PRICE_FRAC),.SEQ_W(SEQ_W),.REV_W(REV_W)
    ) dut (.*);

    wire [CONTROL_W-1:0] actual_control={c_rsp_status,c_rsp_venue,c_rsp_revision,c_rsp_sequence,
                                          c_rsp_turnover,c_rsp_armed};
    wire [REPLY_W-1:0] actual_fill={m_status,m_venue,m_tag,m_revision,m_sequence,m_gross,
                                    m_fee,m_cash,m_turnover,m_tiers,m_inexact};

    logic [3:0] expected_status;
    logic [REV_W-1:0] expected_revision;
    logic [SEQ_W-1:0] expected_sequence;
    logic [63:0] expected_gross,expected_turnover;
    logic [64:0] expected_fee,expected_cash;
    logic [TIER_W-1:0] expected_tiers;
    logic expected_armed,expected_inexact;
    integer receipts=0,fills=0,positives=0,rebates=0,marginal=0,holds=0;
    integer status_count [0:10];
    string fixture,wave;

    initial begin
        integer source,rc;
        logic fill_type;
        if (!$value$plusargs("vectors=%s",fixture)) $fatal(1,"missing fee oracle tape");
        if ($value$plusargs("wave=%s",wave)) begin
            $dumpfile(wave);
            $dumpvars(0,tb_tiered_fee_settlement);
        end
        source=$fopen(fixture,"r");
        if (!source) $fatal(1,"could not open fee oracle tape");
        for(integer code=0;code<11;code=code+1) status_count[code]=0;
        repeat (3) @(negedge clk);
        rst=0;
        rc=$fscanf(source,"%h",fill_type);
        while (rc==1) begin
            if (!fill_type) begin
                if ($fscanf(source,"%h %h %h %h %h %h %h %h %h %h %h %h %h\n",
                     c_op,c_venue,c_expected_revision,c_next_sequence,c_turnover,c_thresholds,
                     c_maker_rates,c_taker_rates,expected_status,expected_revision,expected_sequence,
                     expected_turnover,expected_armed)!=13)
                    $fatal(1,"bad fee control row");
                c_valid=1;
                @(posedge clk);
                while(!c_ready) @(posedge clk);
                @(negedge clk);
                c_valid=0;
                while(!c_rsp_valid) @(negedge clk);
                repeat(2+receipts%5) begin
                    if(!c_rsp_valid || c_ready || actual_control !==
                        {expected_status,c_venue,expected_revision,expected_sequence,expected_turnover,expected_armed})
                        $fatal(1,"control receipt %0d differs, got %h",receipts,actual_control);
                    holds=holds+1;
                    @(negedge clk);
                end
                c_rsp_ready=1;
                @(negedge clk);
                c_rsp_ready=0;
                receipts=receipts+1;
            end else begin
                if ($fscanf(source,"%h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h\n",
                     s_tag,s_venue,s_revision,s_sequence,s_price,s_quantity,s_maker,s_sell,s_round,
                     expected_status,expected_gross,expected_fee,expected_cash,expected_turnover,
                     expected_tiers,expected_inexact)!=16)
                    $fatal(1,"bad signed fee row");
                s_valid=1;
                @(posedge clk);
                while(!s_ready) @(posedge clk);
                @(negedge clk);
                s_valid=0;
                while(!m_valid) @(negedge clk);
                repeat(2+fills%6) begin
                    if(!m_valid || c_ready || s_ready || actual_fill !==
                        {expected_status,s_venue,s_tag,s_revision,s_sequence,expected_gross,expected_fee,
                         expected_cash,expected_turnover,expected_tiers,expected_inexact})
                        $fatal(1,"fill mismatch tag=%0d q=%0d got=%h fee=%0d expected fee=%0d",
                               s_tag,PRICE_FRAC,actual_fill,m_fee,$signed(expected_fee));
                    holds=holds+1;
                    @(negedge clk);
                end
                positives=positives+(m_fee>0);
                rebates=rebates+(m_fee<0);
                marginal=marginal+(m_tiers>1);
                m_ready=1;
                @(negedge clk);
                m_ready=0;
                fills=fills+1;
            end
            status_count[expected_status]=status_count[expected_status]+1;
            rc=$fscanf(source,"%h",fill_type);
        end
        if(rc!=-1 || fills<20 || receipts<10 || !positives || !rebates || (TIERS>1 && !marginal))
            $fatal(1,"missing fee verification coverage +%0d -%0d multi=%0d",positives,rebates,marginal);
        for(integer code=0;code<11;code=code+1)
            if(!status_count[code]) $fatal(1,"missing status %0d",code);
        $display("PASS tiers=%0d venues=%0d q=%0d controls=%0d fills=%0d positive=%0d rebates=%0d marginal=%0d stalls=%0d",
                 TIERS,VENUES,PRICE_FRAC,receipts,fills,positives,rebates,marginal,holds);
        $finish;
    end

    initial begin
        #2000000;
        $fatal(1,"signed fee verification timed out");
    end
endmodule

`default_nettype wire
