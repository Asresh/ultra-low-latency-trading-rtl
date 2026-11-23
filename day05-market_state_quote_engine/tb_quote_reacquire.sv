`timescale 1ns/1ps
`default_nettype none

module tb_quote_reacquire;
    localparam integer BANKS = 2, EW = 3, RW = 4, PW = 3, QW = 4;
    localparam integer BW = $clog2(BANKS+1), KW = 1+EW+2*PW+QW;
    localparam logic [KW-1:0] A = {1'b1, 3'd1, 3'd1, 3'd5, 4'd4};
    localparam logic [KW-1:0] THIN_A = {1'b1, 3'd1, 3'd1, 3'd5, 4'd1};
    localparam logic [KW-1:0] CLOSED_A = {1'b0, 3'd1, 3'd0, 3'd0, 4'd0};
    localparam logic [KW-1:0] B = {1'b1, 3'd2, 3'd2, 3'd6, 4'd5};
    localparam logic [KW-1:0] NEW_B = {1'b1, 3'd2, 3'd3, 3'd6, 4'd5};

    logic clk = 1'b0;
    always #5 clk = !clk;
    logic rst = 1'b1, q_ready = 1'b0;
    logic [BANKS*KW-1:0] desired_now = '0;
    logic [BANKS*3-1:0] reason_now = '0;
    wire q_valid, q_active, q_current;
    wire [BW-1:0] q_instrument;
    wire [EW-1:0] q_epoch;
    wire [RW-1:0] q_revision;
    wire [PW-1:0] q_bid, q_ask;
    wire [QW-1:0] q_size;
    wire [2:0] q_reason;

    quote_publication_pipeline #(.BANKS(BANKS), .PRICE_W(PW), .QTY_W(QW), .EPOCH_W(EW), .REV_W(RW))
        dut (.*);

    task automatic set_inputs(input logic [KW-1:0] lane_a, lane_b, input bit ready);
        @(negedge clk);
        desired_now = {lane_b, lane_a};
        q_ready = ready;
        reason_now = {3'd0, lane_a[KW-1] ? 3'd0 : 3'd2};
        #1;
    endtask

    task automatic sample_edge;
        @(posedge clk);
        #1;
    endtask

    task automatic drive(input logic [KW-1:0] lane_a, lane_b, input bit ready);
        set_inputs(lane_a, lane_b, ready);
        sample_edge();
    endtask

    task automatic expect_offer(
        input logic [BW-1:0] lane, input logic [KW-1:0] key,
        input logic [RW-1:0] serial, input logic applicable
    );
        if ({q_valid, q_instrument, q_active, q_epoch, q_bid, q_ask, q_size, q_revision, q_current}
            !== {1'b1, lane, key, serial, applicable})
            $fatal(1, "wanted lane %0d key %h revision %0d applicability %b at %0t, got valid %b lane %0d key %h revision %0d applicability %b",
                lane, key, serial, applicable, $time, q_valid, q_instrument,
                {q_active, q_epoch, q_bid, q_ask, q_size}, q_revision, q_current);
        if (q_reason !== 3'd0)
            $fatal(1, "active offer unexpectedly has quote reason %0d", q_reason);
    endtask

    initial begin
        sample_edge();
        @(negedge clk);
        rst = 1'b0;
        drive(A, '0, 1'b0);
        drive(A, B, 1'b0);
        expect_offer(0, A, 0, 1'b1);

        // The consumer discards A when it shrinks. Bank B takes the output
        // while A recovers before the staged shrink can be scheduled.
        set_inputs(THIN_A, B, 1'b1);
        expect_offer(0, A, 0, 1'b0);
        sample_edge();
        expect_offer(1, B, 0, 1'b1);
        drive(A, B, 1'b0);
        drive(A, B, 1'b1);
        expect_offer(0, A, 1, 1'b1);
        drive(A, B, 1'b1);
        if (q_valid)
            $fatal(1, "refresh continued after consumer accepted the current key");

        // The consumer already installed A. Losing eligibility for one edge
        // revokes it even when bank B holds the publication slot throughout.
        drive(A, NEW_B, 1'b0);
        drive(A, NEW_B, 1'b0);
        expect_offer(1, NEW_B, 1, 1'b1);
        drive(CLOSED_A, NEW_B, 1'b0);
        drive(A, NEW_B, 1'b0);
        drive(A, NEW_B, 1'b1);
        expect_offer(0, A, 2, 1'b1);
        drive(A, NEW_B, 1'b1);
        if (q_valid)
            $fatal(1, "gate recovery republished the same current key twice");

        $display("PASS quote reacquisition after stale transfer and hidden eligibility lapse");
        $finish;
    end
endmodule

`default_nettype wire
