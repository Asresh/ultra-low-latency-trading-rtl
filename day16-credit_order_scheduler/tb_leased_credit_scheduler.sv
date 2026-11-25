`timescale 1ns/1ps
`default_nettype none

module tb_leased_credit_scheduler;
    logic clk = 0;
    always #5 clk = ~clk;
    logic rst = 1, s_valid = 0, m_ready = 0, ctl_valid = 0, report_ready = 0;
    logic [1:0] s_class = 0, ctl_class = 0, ctl_op = 0;
    logic [7:0] s_cost = 0, ctl_amount = 0;
    logic [15:0] s_tag = 0, ctl_tag = 0;
    logic [31:0] s_payload = 0;
    logic [3:0] ctl_weight = 0, ctl_lease = 0;
    wire s_ready, s_rejected, m_valid, ctl_ready, report_valid;
    wire [1:0] m_class, report_class, report_op;
    wire [7:0] m_cost, report_capacity, report_available, report_inflight;
    wire [15:0] m_tag, report_tag;
    wire [31:0] m_payload;
    wire [3:0] m_lease, report_lease, report_weight;
    wire [2:0] report_status, lease_active, lease_exhausted;
    wire [1:0] report_queued;

    leased_credit_scheduler #(.DEPTH(3), .LEASE_SLOTS(3), .GEN_W(2)) dut (.*);

    localparam integer ACTION_W = 2+8+16+32+4;
    localparam integer REPORT_W = 2+2+3+8+8+8+2+4+4+16;
    wire [ACTION_W-1:0] action = {m_class,m_cost,m_tag,m_payload,m_lease};
    wire [REPORT_W-1:0] report = {report_op,report_class,report_status,report_capacity,
                                 report_available,report_inflight,report_queued,report_weight,
                                 report_lease,report_tag};
    logic held_order = 0, held_report = 0;
    logic [ACTION_W-1:0] previous_order;
    logic [REPORT_W-1:0] previous_report;
    integer order_count = 0, report_count = 0, order_stalls = 0, report_stalls = 0;

    always @(posedge clk) begin
        if (rst) begin
            held_order <= 0;
            held_report <= 0;
        end else begin
            if (held_order && (!m_valid || action !== previous_order))
                $fatal(1,"held output changed");
            if (held_report && (!report_valid || report !== previous_report))
                $fatal(1,"held report changed");
            held_order <= m_valid && !m_ready;
            held_report <= report_valid && !report_ready;
            previous_order <= action;
            previous_report <= report;
            order_count <= order_count + (m_valid && m_ready);
            report_count <= report_count + (report_valid && report_ready);
            order_stalls <= order_stalls + (m_valid && !m_ready);
            report_stalls <= report_stalls + (report_valid && !report_ready);
        end
    end

    task automatic queue_order(input logic [1:0] lane, input logic [7:0] cost,
                               input logic [15:0] tag);
        @(negedge clk);
        {s_valid,s_class,s_cost,s_tag,s_payload} = {1'b1,lane,cost,tag,16'h9421,tag};
        @(posedge clk);
        if (!s_ready || s_rejected) $fatal(1,"order was not queued");
        @(negedge clk);
        s_valid = 0;
    endtask

    task automatic expect_offer(input logic [1:0] lane, input logic [7:0] cost,
                                input logic [15:0] tag, input logic [3:0] lease);
        while (!m_valid) @(negedge clk);
        #1;
        if (action !== {lane,cost,tag,16'h9421,tag,lease})
            $fatal(1,"wrong offer at %0t class=%0d cost=%0d tag=%x lease=%x",
                   $time,m_class,m_cost,m_tag,m_lease);
    endtask

    task automatic take_order;
        @(negedge clk);
        m_ready = 1;
        @(negedge clk);
        m_ready = 0;
    endtask

    task automatic command(input logic [1:0] op, lane, input logic [7:0] amount,
                           input logic [3:0] weight, lease);
        @(negedge clk);
        {ctl_valid,ctl_op,ctl_class,ctl_amount,ctl_weight,ctl_lease} =
            {1'b1,op,lane,amount,weight,lease};
        ctl_tag = 16'(100 + report_count);
        @(posedge clk);
        if (!ctl_ready) $fatal(1,"control request blocked with no pending report");
        @(negedge clk);
        ctl_valid = 0;
    endtask

    task automatic check_report(input logic [2:0] status, input logic [1:0] lane,
                                input logic [7:0] available, in_flight);
        while (!report_valid) @(negedge clk);
        if (ctl_ready || report_op !== ctl_op || report_tag !== ctl_tag ||
            report_lease !== (ctl_op == 1 ? ctl_lease : 4'd0) ||
            {report_status,report_class,report_available,report_inflight} !==
            {status,lane,available,in_flight})
            $fatal(1,"wrong report at %0t status=%0d class=%0d free=%0d flight=%0d",
                   $time,report_status,report_class,report_available,report_inflight);
    endtask

    task automatic take_report;
        @(negedge clk);
        report_ready = 1;
        @(negedge clk);
        report_ready = 0;
    endtask

    task automatic release_lease(input logic [3:0] lease, input logic [2:0] status,
                                 input logic [1:0] lane, input logic [7:0] available, in_flight);
        // Deliberately supply unrelated fields. The lease owns the refund.
        command(1, 2, 255, 15, lease);
        check_report(status,lane,available,in_flight);
        take_report();
    endtask

    initial begin
        string vcd;
        if ($value$plusargs("wave=%s",vcd)) begin
            $dumpfile(vcd);
            $dumpvars(0,tb_leased_credit_scheduler);
        end
        repeat (2) @(negedge clk);
        rst = 0;
        command(0, 0, 5, 1, 0);
        check_report(0, 0, 5, 0);
        take_report();
        command(0, 1, 8, 1, 0);
        check_report(0, 1, 8, 0);
        take_report();

        queue_order(0, 2, 'h1234);
        expect_offer(0, 2, 'h1234, 4);
        // An offered handle cannot be returned before the order transfers.
        release_lease(4, 6, 0, 0, 0);
        expect_offer(0, 2, 'h1234, 4);
        take_order();
        queue_order(1, 3, 'h1234);
        expect_offer(1, 3, 'h1234, 5);
        take_order();
        queue_order(0, 1, 'habcd);
        expect_offer(0, 1, 'habcd, 6);
        take_order();
        queue_order(1, 2, 'hffff);
        if (lease_active !== 3'b111 || m_valid) $fatal(1,"full leases offered an order");

        command(1, 0, 0, 0, 5);
        check_report(0, 1, 8, 0);
        repeat (5) begin
            @(negedge clk);
            if (m_valid || lease_active !== 3'b111) $fatal(1,"unobserved refund retired");
        end
        take_report();
        expect_offer(1, 2, 'hffff, 9);
        release_lease(4, 0, 0, 4, 1);
        expect_offer(1, 2, 'hffff, 9);
        take_order();
        release_lease(4, 6, 0, 0, 0);
        release_lease(5, 6, 0, 0, 0);
        release_lease(7, 5, 0, 0, 0);
        command(2, 1, 0, 0, 0);
        check_report(0, 1, 6, 2);
        take_report();
        release_lease(6, 0, 0, 5, 0);
        release_lease(9, 0, 1, 8, 0);

        queue_order(0, 5, 16);
        expect_offer(0, 5, 16, 8);
        take_order();
        release_lease(8, 0, 0, 5, 0);
        queue_order(0, 5, 16);
        expect_offer(0, 5, 16, 12);
        take_order();
        if (lease_exhausted !== 3'b001) $fatal(1,"terminal generation was hidden");
        release_lease(12, 0, 0, 5, 0);
        queue_order(0, 5, 17);
        expect_offer(0, 5, 17, 13);
        take_order();
        release_lease(13, 0, 0, 5, 0);
        queue_order(0, 5, 18);
        expect_offer(0, 5, 18, 10);
        take_order();
        release_lease(10, 0, 0, 5, 0);
        queue_order(0, 5, 19);
        expect_offer(0, 5, 19, 14);
        take_order();
        release_lease(14, 0, 0, 5, 0);
        queue_order(0, 5, 20);
        repeat (5) begin
            @(negedge clk);
            if (m_valid || lease_active || lease_exhausted !== 3'b111)
                $fatal(1,"generation wrapped after exhaustion");
        end
        command(2, 0, 0, 0, 0);
        check_report(0, 0, 5, 0);
        if (report_queued !== 1) $fatal(1,"exhausted offer escaped the queue");
        take_report();

        command(0, 0, 7, 1, 0);
        check_report(3, 0, 5, 0);
        take_report();
        if (order_count != 9 || report_count != 18 || order_stalls < 12 || report_stalls < 10)
            $fatal(1,"unexpected coverage counts %d %d %d %d",
                   order_count,report_count,order_stalls,report_stalls);
        rst = 1;
        @(negedge clk);
        rst = 0;
        command(0, 0, 2, 1, 0);
        check_report(0, 0, 2, 0);
        take_report();
        queue_order(0, 2, 30);
        expect_offer(0, 2, 30, 4);
        take_order();
        queue_order(0, 2, 31);
        if (m_valid) $fatal(1, "credit was not charged");
        command(1, 0, 0, 0, 4);
        check_report(0, 0, 2, 0);
        // Credit is available before the acknowledged slot can be recycled.
        expect_offer(0, 2, 31, 5);
        take_order();
        if (lease_active !== 3'b011) $fatal(1, "refund report retired its slot early");
        take_report();
        if (lease_active !== 3'b010) $fatal(1, "refund report did not retire its slot");
        release_lease(5, 0, 0, 2, 0);
        if (order_count != 11 || report_count != 21)
            $fatal(1, "missing one of the simultaneous-boundary checks");
        $display("PASS leased scheduler (%0d orders, %0d control reports, %0d/%0d held cycles)",
                 order_count,report_count,order_stalls,report_stalls);
        $finish;
    end

    initial begin
        #20000;
        $fatal(1,"leased scheduler timeout");
    end
endmodule

`default_nettype wire
