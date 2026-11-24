`timescale 1ns/1ps
`default_nettype none

module tb_risk_boundaries;
    localparam integer PRICE_W = 8;
    localparam integer QTY_W = 8;
    localparam integer NOTIONAL_W = 16;
    localparam integer TAG_W = 8;
    localparam integer GEN_W = 3;
    localparam integer EVENT_W = 4;
    localparam integer SLOT_W = 2;
    localparam integer COUNT_W = 3;
    localparam [2:0] NEW_ORDER = 0, FILL = 1, CANCEL = 2, SNAPSHOT = 5;
    localparam [4:0] OK = 0, DISABLED = 11, HALTED = 19, LEDGER_INVARIANT = 20;

    logic clk = 0;
    always #5 clk = ~clk;
    logic rst, halt_req;
    wire halted;
    logic cfg_valid, cfg_enable;
    wire cfg_ready;
    logic [PRICE_W-1:0] cfg_min_price, cfg_max_price;
    logic [QTY_W-1:0] cfg_max_order_qty;
    logic [NOTIONAL_W-1:0] cfg_max_open_notional;
    logic [COUNT_W-1:0] cfg_max_open_orders;

    logic s_valid;
    wire s_ready;
    logic [2:0] s_op;
    logic [TAG_W-1:0] s_tag;
    logic [SLOT_W-1:0] s_slot;
    logic [GEN_W-1:0] s_generation;
    logic [EVENT_W-1:0] s_event_seq;
    logic [PRICE_W-1:0] s_price;
    logic [QTY_W-1:0] s_qty;

    wire m_valid;
    logic m_ready;
    wire [2:0] m_op;
    wire [TAG_W-1:0] m_tag;
    wire [SLOT_W-1:0] m_slot;
    wire [GEN_W-1:0] m_generation;
    wire [EVENT_W-1:0] m_event_seq;
    wire [4:0] m_code;
    wire m_slot_live;
    wire [QTY_W-1:0] m_leaves;
    wire [PRICE_W-1:0] m_limit_price;
    wire [NOTIONAL_W-1:0] m_reserved, m_open_notional, m_executed_notional;
    wire [COUNT_W-1:0] m_open_orders;

    hft_order_risk_gateway #(
        .NUM_ACCOUNTS(1), .NUM_INSTRUMENTS(1), .ORDER_SLOTS(4),
        .TAG_W(TAG_W), .PRICE_W(PRICE_W), .QTY_W(QTY_W), .GEN_W(GEN_W), .EVENT_W(EVENT_W)
    ) dut (
        .clk, .rst, .halt_req, .halted,
        .cfg_valid, .cfg_ready, .cfg_account(1'b0), .cfg_instrument(1'b0),
        .cfg_sell(1'b0), .cfg_enable, .cfg_min_price, .cfg_max_price,
        .cfg_max_order_qty, .cfg_max_open_notional, .cfg_max_open_orders,
        .s_valid, .s_ready, .s_op, .s_tag, .s_account(1'b0),
        .s_instrument(1'b0), .s_sell(1'b0), .s_slot, .s_generation,
        .s_event_seq, .s_price, .s_qty,
        .m_valid, .m_ready, .m_op, .m_tag, .m_account(), .m_instrument(), .m_sell(),
        .m_slot, .m_generation, .m_event_seq, .m_code, .m_slot_live,
        .m_leaves, .m_limit_price, .m_reserved, .m_open_notional,
        .m_executed_notional, .m_open_orders
    );

    integer observed = 0;
    integer issued = 0;
    time accepted_at;
    localparam integer PAYLOAD_W = 3 + TAG_W + SLOT_W + GEN_W + EVENT_W + 5 + 1 +
                                    PRICE_W + QTY_W + 3 * NOTIONAL_W + COUNT_W;
    wire [PAYLOAD_W-1:0] reply = {
        m_op, m_tag, m_slot, m_generation, m_event_seq, m_code, m_slot_live,
        m_leaves, m_limit_price, m_reserved, m_open_notional, m_executed_notional, m_open_orders
    };

    bit prior_stall = 0;
    logic [PAYLOAD_W-1:0] stalled_reply;
    always @(posedge clk) begin
        if (rst) begin
            prior_stall = 0;
            if (m_valid || s_ready || cfg_ready)
                $fatal(1, "reset leaked channel activity");
        end else begin
            if (prior_stall && (!m_valid || reply !== stalled_reply))
                $fatal(1, "response changed while the sink held ready low");
            prior_stall = m_valid && !m_ready;
            stalled_reply = reply;
            if (s_valid && s_ready)
                issued = issued + 1;
            if (m_valid && m_ready)
                observed = observed + 1;
            if (cfg_valid && s_ready)
                $fatal(1, "configuration lost input priority");
        end
    end

    // Apply the supplied risk operation and identity fields to the request pins.
    task automatic drive_order(
        input logic [2:0] op,
        input integer tag, slot, generation, seq_num, price, qty
    );
        begin
            s_op = op;
            s_tag = TAG_W'(tag);
            s_slot = SLOT_W'(slot);
            s_generation = GEN_W'(generation);
            s_event_seq = EVENT_W'(seq_num);
            s_price = PRICE_W'(price);
            s_qty = QTY_W'(qty);
            s_valid = 1;
        end
    endtask

    // Wait for the current request handshake at the active edge.
    task automatic wait_issue;
        begin
            do @(posedge clk); while (!s_ready);
            accepted_at = $time;
            @(negedge clk);
            s_valid = 0;
        end
    endtask

    // Drive an order and wait until the request has been admitted to the pipeline.
    task automatic issue(
        input logic [2:0] op,
        input integer tag, slot, generation, seq_num, price, qty
    );
        begin
            @(negedge clk);
            drive_order(op, tag, slot, generation, seq_num, price, qty);
            wait_issue();
        end
    endtask

    // Set the risk policy inputs using the requested enable and exposure limits.
    task automatic drive_policy(input logic enable, input integer cap, count);
        begin
            cfg_enable = enable;
            cfg_min_price = 1;
            cfg_max_price = 255;
            cfg_max_order_qty = 255;
            cfg_max_open_notional = 16'(cap);
            cfg_max_open_orders = COUNT_W'(count);
            cfg_valid = 1;
        end
    endtask

    // Commit the prepared policy before exercising the request path.
    task automatic configure;
        begin
            @(negedge clk);
            drive_policy(1, 65535, 4);
            do @(posedge clk); while (!cfg_ready);
            @(negedge clk);
            cfg_valid = 0;
        end
    endtask

    // Wait for one result and check its status and visible accounting fields.
    task automatic expect_reply(
        input logic [2:0] op,
        input integer tag, slot, generation, seq_num,
        input logic [4:0] status,
        input logic live,
        input integer leaves, limit_price, reserve, open_amount, executed, count
    );
        logic [PAYLOAD_W-1:0] expected_reply;
        begin
            do @(negedge clk); while (!m_valid);
            expected_reply = {
                op, TAG_W'(tag), SLOT_W'(slot), GEN_W'(generation),
                EVENT_W'(seq_num), status, live, QTY_W'(leaves), PRICE_W'(limit_price),
                NOTIONAL_W'(reserve), NOTIONAL_W'(open_amount), NOTIONAL_W'(executed), COUNT_W'(count)
            };
            if (reply !== expected_reply)
                $fatal(1, "tag %0d expected %h, received %h", tag, expected_reply, reply);
            m_ready = 1;
            @(posedge clk);
            @(negedge clk);
            m_ready = 0;
        end
    endtask

    // Pulse reset and initialize the gateway controls for a directed case.
    task automatic reset_gateway;
        begin
            @(negedge clk);
            rst = 1;
            s_valid = 0;
            cfg_valid = 0;
            halt_req = 0;
            m_ready = 0;
            repeat (2) @(negedge clk);
            rst = 0;
            #1;
            if (!cfg_ready || !s_ready || m_valid || halted)
                $fatal(1, "bad idle after reset");
        end
    endtask

    // Exercise request and policy interleavings under a full response path.
    task automatic test_fifo_and_config;
        time first_issue;
        begin
            configure();
            issue(NEW_ORDER, 1, 0, 0, 0, 10, 4);
            first_issue = accepted_at;
            if (m_valid) $fatal(1, "combinational or zero-cycle reply");
            @(negedge clk);
            drive_order(NEW_ORDER, 2, 1, 0, 0, 9, 5);
            if (m_valid || s_ready) $fatal(1, "phase 1 appeared early");
            @(negedge clk);
            if (m_valid || s_ready) $fatal(1, "phase 2 appeared early");
            @(negedge clk);
            if (!m_valid || m_tag != 1 || !s_ready)
                $fatal(1, "first decision latency or issue interval wrong");
            wait_issue();
            if (accepted_at != first_issue + 40)
                $fatal(1, "requests with available FIFO space must issue four cycles apart");

            @(negedge clk);
            drive_order(NEW_ORDER, 3, 2, 0, 0, 5, 2);
            repeat (6) begin
                @(posedge clk);
                if (s_ready || !m_valid || m_tag != 1)
                    $fatal(1, "full queue did not stall the third request");
            end

            expect_reply(NEW_ORDER, 1, 0, 1, 0, OK, 1, 4, 10, 40, 40, 0, 1);
            if (issued != 3)
                $fatal(1, "a simultaneous pop from full did not free an issue credit");
            s_valid = 0;
            repeat (4) @(negedge clk);
            if (!m_valid || m_tag != 2) $fatal(1, "older reply overwritten");

            drive_policy(0, 100, 2);
            drive_order(NEW_ORDER, 4, 3, 0, 0, 11, 4);
            repeat (3) begin
                @(posedge clk);
                if (cfg_ready || s_ready || m_tag != 2)
                    $fatal(1, "configuration did not block behind old responses");
            end
            expect_reply(NEW_ORDER, 2, 1, 1, 0, OK, 1, 5, 9, 45, 85, 0, 2);
            expect_reply(NEW_ORDER, 3, 2, 1, 0, OK, 1, 2, 5, 10, 95, 0, 3);
            #1;
            if (!cfg_ready || s_ready) $fatal(1, "configuration lost barrier priority");
            @(posedge clk);
            @(negedge clk);
            cfg_valid = 0;
            wait_issue();
            expect_reply(NEW_ORDER, 4, 3, 0, 0, DISABLED, 0, 0, 0, 0, 95, 0, 3);
        end
    endtask

    // Verify the emergency stop around the pending request's commit edge.
    task automatic test_commit_halt;
        begin
            reset_gateway();
            configure();
            issue(NEW_ORDER, 10, 0, 0, 0, 12, 8);
            issue(NEW_ORDER, 11, 1, 0, 0, 12, 8);
            repeat (2) @(negedge clk);
            halt_req = 1;
            @(negedge clk);
            halt_req = 0;
            #1;
            if (!halted) $fatal(1, "commit-edge halt did not latch");

            expect_reply(NEW_ORDER, 10, 0, 1, 0, OK, 1, 8, 12, 96, 96, 0, 1);
            expect_reply(NEW_ORDER, 11, 1, 0, 0, HALTED, 0, 0, 0, 0, 96, 0, 1);
            issue(FILL, 12, 0, 1, 1, 11, 3);
            expect_reply(FILL, 12, 0, 1, 1, OK, 1, 5, 12, 60, 60, 33, 1);
        end
    endtask

    // Apply reset while a request or response is in flight.
    task automatic test_active_reset;
        begin
            reset_gateway();
            configure();
            issue(NEW_ORDER, 20, 0, 0, 0, 10, 12);
            issue(NEW_ORDER, 21, 1, 0, 0, 12, 14);
            // Reply 20 is already committed, and operation 21 is in flight.
            if (!m_valid || m_tag != 20) $fatal(1, "reset test did not create pending work");
            reset_gateway();
            repeat (7) begin
                @(negedge clk);
                if (m_valid) $fatal(1, "reset leaked a pre-reset response");
            end
            issue(SNAPSHOT, 22, 0, 0, 0, 0, 0);
            expect_reply(SNAPSHOT, 22, 0, 0, 0, OK, 0, 0, 0, 0, 0, 0, 0);
            issue(NEW_ORDER, 23, 1, 0, 0, 12, 14);
            expect_reply(NEW_ORDER, 23, 1, 0, 0, DISABLED, 0, 0, 0, 0, 0, 0, 0);
        end
    endtask

    // Check admission and lifecycle boundary cases that reject invalid state changes.
    task automatic test_fault_paths;
        begin
            reset_gateway();
            configure();
            issue(NEW_ORDER, 30, 0, 0, 0, 9, 16);
            expect_reply(NEW_ORDER, 30, 0, 1, 0, OK, 1, 16, 9, 144, 144, 0, 1);

            // Corrupt the captured ledger only. The committed table must remain intact.
            issue(CANCEL, 31, 0, 1, 1, 0, 0);
            @(negedge clk);
            force dut.ledger_q = '0;
            repeat (2) @(negedge clk);
            release dut.ledger_q;
            if (!halted) $fatal(1, "corrupt accounting did not latch halt");
            expect_reply(CANCEL, 31, 0, 1, 0, LEDGER_INVARIANT, 1, 16, 9, 144, 0, 0, 0);

            issue(SNAPSHOT, 32, 0, 0, 0, 0, 0);
            expect_reply(SNAPSHOT, 32, 0, 1, 0, OK, 1, 16, 9, 144, 144, 0, 1);
            issue(CANCEL, 33, 0, 1, 1, 0, 0);
            expect_reply(CANCEL, 33, 0, 1, 1, OK, 0, 0, 9, 0, 0, 0, 0);
        end
    endtask

    // A requested capture records checked simulation signals for the README.
    initial begin : optional_waveform
        string wave_path;
        if ($value$plusargs("wave=%s", wave_path)) begin
            $dumpfile(wave_path);
            $dumpvars(2, tb_risk_boundaries);
        end
    end

    initial begin
        rst = 1;
        halt_req = 0;
        cfg_valid = 0;
        s_valid = 0;
        m_ready = 0;
        repeat (2) @(negedge clk);
        rst = 0;
        test_fifo_and_config();
        test_commit_halt();
        test_active_reset();
        test_fault_paths();
        if (issued != observed + 2)
            $fatal(1, "expected exactly two flushed commands, issued %0d observed %0d", issued, observed);
        $display("PASS risk protocol: fixed latency, held response, policy barrier, commit-edge halt, active reset and injected invariant fault");
        $finish;
    end

    initial begin
        #10000;
        $fatal(1, "protocol test watchdog");
    end
endmodule

`default_nettype wire
