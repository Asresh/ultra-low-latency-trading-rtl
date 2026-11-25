`timescale 1ns/1ps
`default_nettype none

module tb_notional_basket #(
    parameter integer PRICE_FRAC = 8,
    parameter integer MAX_LEGS = 8
);
    localparam integer COUNT_W = $clog2(MAX_LEGS+2);
    localparam integer ACC_W = 65 + $clog2(MAX_LEGS);
    localparam integer RESPONSE_W = 3*ACC_W + 2*COUNT_W + 21;
    logic clk = 0;
    always #5 clk = !clk;

    logic rst = 1, req_valid = 0, req_debit_en, req_credit_en;
    wire req_ready;
    logic [15:0] req_tag;
    logic [COUNT_W-1:0] req_count;
    logic [63:0] req_max_debit, req_min_credit;
    logic leg_valid = 0, leg_last, leg_sell;
    wire leg_ready;
    logic [31:0] leg_price, leg_qty;
    logic [15:0] leg_fee_bps;
    logic [1:0] leg_round;

    wire rsp_valid;
    logic rsp_ready = 0;
    wire [15:0] rsp_tag;
    wire [2:0] rsp_status, rsp_leg_status;
    wire [COUNT_W-1:0] rsp_count, rsp_bad_leg;
    wire [ACC_W-2:0] rsp_gross, rsp_fee;
    wire signed [ACC_W-1:0] rsp_net_cash;
    wire rsp_inexact;
    wire [RESPONSE_W-1:0] response = {rsp_tag,rsp_status,rsp_count,rsp_bad_leg,
                                    rsp_leg_status,rsp_gross,rsp_fee,rsp_net_cash,rsp_inexact};

    notional_basket #(.MAX_LEGS(MAX_LEGS), .PRICE_FRAC(PRICE_FRAC)) dut (.*);
    integer fixture, rc, actual_count, basket_index = 0, legs = 0;
    integer statuses [0:4];
    integer valid_stalls = 0, debit_trials = 0, credit_trials = 0;
    logic [2:0] expected_status, expected_leg_status;
    logic [COUNT_W-1:0] expected_count, expected_bad_leg;
    logic [ACC_W-2:0] expected_gross, expected_fee;
    logic [ACC_W-1:0] expected_cash;
    logic expected_inexact;
    logic [RESPONSE_W-1:0] expected;
    string tape, vcd;

    always @(posedge clk) begin
        if (!rst) begin
            if (req_valid && req_ready && leg_ready)
                $fatal(1,"leg accepted in idle");
            if (rsp_valid && (req_ready || leg_ready))
                $fatal(1,"outstanding result allowed a new transaction");
        end
    end

    initial begin
        if (!$value$plusargs("tape=%s", tape))
            $fatal(1,"missing basket tape");
        if ($value$plusargs("wave=%s", vcd)) begin
            $dumpfile(vcd);
            $dumpvars(0,tb_notional_basket);
        end
        fixture = $fopen(tape,"r");
        if (!fixture) $fatal(1,"could not open basket vectors");
        for (integer i = 0; i < 5; i=i+1) statuses[i] = 0;
        repeat (2) @(negedge clk);
        rst = 0;
        rc = $fscanf(fixture,"%h %h %h %h %h %h %h\n",req_tag,req_count,
                     req_debit_en,req_max_debit,req_credit_en,req_min_credit,actual_count);
        while (rc != -1) begin
            if (rc != 7 || actual_count == 0) $fatal(1,"invalid basket header");
            req_valid = 1;
            @(posedge clk);
            while (!req_ready) @(posedge clk);
            @(negedge clk);
            req_valid = 0;
            for (integer index = 0; index < actual_count; index=index+1) begin
                if ($fscanf(fixture,"%h %h %h %h %h\n",
                            leg_price,leg_qty,leg_fee_bps,leg_round,leg_sell) != 5)
                    $fatal(1,"invalid leg row");
                leg_valid = 1;
                leg_last = index == actual_count-1;
                @(posedge clk);
                while (!leg_ready) begin
                    if (rsp_valid) $fatal(1,"result preceded final accepted leg");
                    @(posedge clk);
                end
                @(negedge clk);
                leg_valid = 0;
                legs = legs + 1;
                repeat ((basket_index+index)%3) @(negedge clk);
            end
            if ($fscanf(fixture,"%h %h %h %h %h %h %h %h\n",expected_status,
                        expected_count,expected_bad_leg,expected_leg_status,
                        expected_gross,expected_fee,expected_cash,expected_inexact) != 8)
                $fatal(1,"invalid expected response");
            expected = {req_tag,expected_status,expected_count,expected_bad_leg,
                        expected_leg_status,expected_gross,expected_fee,expected_cash,expected_inexact};
            while (!rsp_valid) @(negedge clk);
            repeat (3 + basket_index%6) begin
                if (!rsp_valid || response !== expected)
                    $fatal(1,"basket=%0d expected=%h got=%h",basket_index,expected,response);
                valid_stalls = valid_stalls + 1;
                @(negedge clk);
            end
            rsp_ready = 1;
            @(posedge clk);
            if (!rsp_valid || response !== expected) $fatal(1,"basket transfer changed");
            @(negedge clk);
            rsp_ready = 0;
            if (rsp_valid) $fatal(1,"duplicate basket response");
            statuses[expected_status] = statuses[expected_status] + 1;
            basket_index = basket_index+1;
            debit_trials = debit_trials + req_debit_en;
            credit_trials = credit_trials + req_credit_en;
            rc = $fscanf(fixture,"%h %h %h %h %h %h %h\n",req_tag,req_count,
                         req_debit_en,req_max_debit,req_credit_en,req_min_credit,actual_count);
        end
        $display("PASS basket fraction=%0d maximum=%0d baskets=%0d legs=%0d stalls=%0d limits=%0d/%0d statuses=%0d,%0d,%0d,%0d,%0d",
                 PRICE_FRAC,MAX_LEGS,basket_index,legs,valid_stalls,debit_trials,credit_trials,
                 statuses[0],statuses[1],statuses[2],statuses[3],statuses[4]);
        $finish;
    end

    initial begin
        #8000000;
        $fatal(1,"basket scoreboard timeout");
    end
endmodule

`default_nettype wire
