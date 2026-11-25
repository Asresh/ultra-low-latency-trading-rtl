`timescale 1ns/1ps
`default_nettype none

module tb_quote_order_bridge;
    localparam integer INST_W = 2, PRICE_W = 8, QTY_W = 4;
    localparam integer EPOCH_W = 3, GEN_W = 2, REQUEST_W = 3;
    localparam integer PAYLOAD_W = INST_W + PRICE_W + QTY_W + EPOCH_W + GEN_W + REQUEST_W + 2;
    logic clk = 0;
    always #5 clk = ~clk;
    logic rst = 1;
    logic s_valid = 0, s_active = 0, s_current = 0;
    wire s_ready;
    logic [INST_W-1:0] s_instrument = 0;
    logic [EPOCH_W-1:0] s_epoch = 0;
    logic [PRICE_W-1:0] s_bid = 0, s_ask = 0;
    logic [QTY_W-1:0] s_size = 0;
    logic [1:0] eligible = 2'b11;
    logic [2*EPOCH_W-1:0] epoch = {3'd2,3'd1};

    wire m_valid, m_applicable, m_cancel, m_ask;
    logic m_ready = 0;
    wire [INST_W-1:0] m_instrument;
    wire [GEN_W-1:0] m_generation;
    wire [REQUEST_W-1:0] m_request;
    wire [EPOCH_W-1:0] m_epoch;
    wire [PRICE_W-1:0] m_price;
    wire [QTY_W-1:0] m_size;

    logic ack_valid = 0, ack_cancel = 0, ack_ask = 0, ack_ok = 0;
    wire ack_ready;
    logic [INST_W-1:0] ack_instrument = 0;
    logic [GEN_W-1:0] ack_generation = 0;
    logic [REQUEST_W-1:0] ack_request = 0;
    logic rearm_valid = 0;
    logic [INST_W-1:0] rearm_instrument = 0;
    wire [3:0] rejected, exhausted;
    wire [31:0] stale_intents, bad_intents, aborted_adds, stale_acks;

    quote_order_bridge #(
        .INSTRUMENTS(2), .PRICE_W(PRICE_W), .QTY_W(QTY_W),
        .EPOCH_W(EPOCH_W), .GEN_W(GEN_W), .REQUEST_W(REQUEST_W)
    ) dut (.*);

    wire [PAYLOAD_W-1:0] payload = {m_cancel, m_instrument, m_ask, m_generation,
                                    m_request, m_epoch, m_price, m_size};
    logic held = 0;
    logic [PAYLOAD_W-1:0] old_payload = 0;
    integer executed = 0, waived = 0, stalls = 0;
    always @(posedge clk) begin
        if (rst) begin
            held <= 0;
        end else begin
            if (held && (!m_valid || payload !== old_payload))
                $fatal(1, "held order payload changed");
            held <= m_valid && !m_ready;
            old_payload <= payload;
            if (m_valid) begin
                if (!m_ready)
                    stalls <= stalls + 1;
                else if (m_applicable)
                    executed <= executed + 1;
                else
                    waived <= waived + 1;
                if (m_cancel && !m_applicable)
                    $fatal(1, "cancel was made inapplicable");
            end
        end
    end

    task automatic quote(
        input logic [INST_W-1:0] instrument,
        input logic [EPOCH_W-1:0] revision,
        input logic active, current_quote,
        input logic [PRICE_W-1:0] bid, ask,
        input logic [QTY_W-1:0] amount
    );
        @(negedge clk);
        {s_valid,s_instrument,s_epoch,s_active,s_current,s_bid,s_ask,s_size} =
            {1'b1,instrument,revision,active,current_quote,bid,ask,amount};
        @(posedge clk);
        if (!s_ready) $fatal(1, "unexpected quote backpressure");
        @(negedge clk);
        s_valid = 0;
    endtask

    task automatic expect_action(
        input logic cancel,
        input logic [INST_W-1:0] instrument,
        input logic ask,
        input logic [GEN_W-1:0] generation,
        input logic [REQUEST_W-1:0] request,
        input logic [EPOCH_W-1:0] revision,
        input logic [PRICE_W-1:0] order_price,
        input logic [QTY_W-1:0] amount,
        input logic applicable
    );
        while (!m_valid) @(negedge clk);
        if (payload !== {cancel,instrument,ask,generation,request,revision,order_price,amount} ||
            m_applicable !== applicable)
            $fatal(1, "wrong action time=%0t canc=%b inst=%0d ask=%b gen=%0d req=%0d epoch=%0d price=%0d size=%0d app=%b",
                   $time,m_cancel,m_instrument,m_ask,m_generation,m_request,m_epoch,m_price,m_size,m_applicable);
    endtask

    task automatic take_action;
        @(negedge clk);
        m_ready = 1;
        @(negedge clk);
        m_ready = 0;
    endtask

    task automatic acknowledge(
        input logic cancel,
        input logic [INST_W-1:0] instrument,
        input logic ask,
        input logic [GEN_W-1:0] generation,
        input logic [REQUEST_W-1:0] request,
        input logic success
    );
        @(negedge clk);
        {ack_valid,ack_cancel,ack_instrument,ack_ask,ack_generation,ack_request,ack_ok} =
            {1'b1,cancel,instrument,ask,generation,request,success};
        @(posedge clk);
        if (!ack_ready) $fatal(1, "unexpected acknowledgment backpressure");
        @(negedge clk);
        ack_valid = 0;
    endtask

    task automatic quiet(input integer cycles);
        repeat (cycles) begin
            @(negedge clk);
            if (m_valid) $fatal(1, "unexpected order action at %0t", $time);
        end
    endtask

    initial begin
        string vcd;
        if ($value$plusargs("wave=%s", vcd)) begin
            $dumpfile(vcd);
            $dumpvars(0, tb_quote_order_bridge);
        end
        repeat (2) @(negedge clk);
        rst = 0;

        quote(0, 1, 1, 1, 100, 104, 5);
        expect_action(0, 0, 0, 1, 1, 1, 100, 5, 1);
        quote(0, 1, 1, 1, 101, 104, 5);
        expect_action(0, 0, 0, 1, 1, 1, 100, 5, 0);
        take_action();

        expect_action(0, 0, 1, 1, 1, 1, 104, 5, 1);
        take_action();
        acknowledge(0, 0, 1, 1, 2, 1);
        acknowledge(0, 0, 1, 1, 1, 1);
        expect_action(0, 0, 0, 1, 1, 1, 101, 5, 1);
        take_action();
        acknowledge(0, 0, 0, 1, 1, 1);
        quote(0, 0, 0, 1, 0, 0, 0);
        quote(0, 1, 0, 0, 0, 0, 0);
        quiet(3);

        quote(0, 1, 1, 1, 105, 108, 7);
        expect_action(1, 0, 1, 1, 2, 1, 104, 5, 1);
        take_action();
        acknowledge(1, 0, 1, 1, 2, 0);
        if (rejected !== 4'b0010) $fatal(1, "cancel rejection was lost");
        expect_action(1, 0, 0, 1, 2, 1, 101, 5, 1);
        take_action();
        acknowledge(1, 0, 0, 1, 2, 1);
        // Replacing the bid here would cross the still-live ask at 104.
        quiet(4);
        rearm_valid = 1;
        @(negedge clk);
        rearm_valid = 0;
        expect_action(1, 0, 1, 1, 3, 1, 104, 5, 1);
        take_action();
        acknowledge(1, 0, 1, 1, 2, 1);
        quiet(4);
        acknowledge(1, 0, 1, 1, 3, 1);
        expect_action(0, 0, 0, 2, 3, 1, 105, 7, 1);
        take_action();
        acknowledge(1, 0, 0, 1, 2, 1);
        acknowledge(0, 0, 0, 2, 3, 1);

        expect_action(0, 0, 1, 2, 4, 1, 108, 7, 1);
        // A withdrawal and the old ADD can transfer on the very same edge.
        @(negedge clk);
        {m_ready,s_valid,s_instrument,s_epoch,s_active,s_current} = {1'b1,1'b1,2'd0,3'd1,1'b0,1'b1};
        #1;
        if (m_applicable !== 1'b0) $fatal(1, "withdrawal did not invalidate the same edge");
        @(negedge clk);
        {m_ready,s_valid} = 0;
        expect_action(1, 0, 0, 2, 4, 1, 105, 7, 1);
        take_action();
        acknowledge(1, 0, 0, 2, 4, 1);
        quiet(3);

        quote(0, 1, 1, 1, 116, 112, 3);
        quiet(3);
        quote(3, 1, 0, 1, 0, 0, 0);
        quote(1, 2, 1, 1, 110, 115, 3);
        expect_action(0, 1, 0, 1, 1, 2, 110, 3, 1);
        epoch[3+:3] = 3;
        #1;
        expect_action(0, 1, 0, 1, 1, 2, 110, 3, 0);
        take_action();
        quiet(3);
        quote(1, 3, 1, 1, 110, 115, 3);
        expect_action(0, 1, 1, 1, 1, 3, 115, 3, 1);
        take_action();
        acknowledge(0, 1, 1, 1, 1, 0);
        expect_action(0, 1, 0, 1, 1, 3, 110, 3, 1);
        take_action();
        acknowledge(0, 1, 0, 1, 1, 1);
        eligible[1] = 0;
        expect_action(1, 1, 0, 1, 2, 3, 110, 3, 1);
        take_action();
        acknowledge(1, 1, 0, 1, 2, 1);
        eligible[1] = 1;
        quiet(3);

        quote(0, 1, 1, 1, 105, 108, 7);
        expect_action(0, 0, 0, 3, 5, 1, 105, 7, 1);
        take_action();
        acknowledge(0, 0, 0, 3, 5, 1);
        expect_action(0, 0, 1, 2, 4, 1, 108, 7, 1);
        take_action();
        acknowledge(0, 0, 1, 2, 4, 1);
        if (!exhausted[0]) $fatal(1, "terminal generation was not reported");
        quote(0, 1, 0, 1, 0, 0, 0);
        expect_action(1, 0, 0, 3, 6, 1, 105, 7, 1);
        take_action();
        acknowledge(1, 0, 0, 3, 6, 1);
        expect_action(1, 0, 1, 2, 5, 1, 108, 7, 1);
        take_action();
        acknowledge(1, 0, 1, 2, 5, 1);
        quiet(3);
        if ({stale_intents,bad_intents,aborted_adds,stale_acks} !== {32'd2,32'd2,32'd3,32'd3})
            $fatal(1, "fault accounting mismatch: %d %d %d %d", stale_intents,bad_intents,aborted_adds,stale_acks);
        if (executed != 14 || waived != 3 || stalls < 10)
            $fatal(1, "coverage mismatch: %d %d %d", executed,waived,stalls);
        // A rearm sampled with the rejection authorizes the next request immediately.
        quote(1, 3, 1, 1, 110, 115, 3);
        expect_action(0, 1, 0, 2, 3, 3, 110, 3, 1);
        take_action();
        acknowledge(0, 1, 0, 2, 3, 1);
        expect_action(0, 1, 1, 2, 2, 3, 115, 3, 1);
        eligible[1] = 0;
        take_action();
        expect_action(1, 1, 0, 2, 4, 3, 110, 3, 1);
        take_action();
        @(negedge clk);
        {ack_valid,ack_cancel,ack_instrument,ack_ask,ack_generation,ack_request,ack_ok} =
            {1'b1,1'b1,2'd1,1'b0,2'd2,3'd4,1'b0};
        rearm_valid = 1;
        rearm_instrument = 1;
        @(negedge clk);
        {ack_valid,rearm_valid} = 0;
        if (rejected[2]) $fatal(1, "coincident rearm was lost");
        expect_action(1, 1, 0, 2, 5, 3, 110, 3, 1);
        take_action();
        acknowledge(1, 1, 0, 2, 5, 1);
        quiet(3);
        $display("PASS quote order bridge (%0d actions, %0d invalidated offers, %0d held cycles)",
                 executed, waived, stalls);
        $finish;
    end

    initial begin
        #20000;
        $fatal(1, "quote bridge test timeout");
    end
endmodule
`default_nettype wire
