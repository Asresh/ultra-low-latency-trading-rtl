`timescale 1ns/1ps
`default_nettype none

module pretrade_risk_gateway_tb;
    localparam integer NUM_INSTRUMENTS = 3;
    localparam integer PRICE_W = 8;
    localparam integer QTY_W = 8;
    localparam integer TAG_W = 16;
    localparam integer INSTRUMENT_W = 2;
    localparam integer NOTIONAL_W = PRICE_W + QTY_W;
    localparam integer QUEUE_DEPTH = 4096;

    logic clk = 0;
    always #5 clk = ~clk;

    logic rst, halt_req, halted;
    logic cfg_valid, cfg_ready, cfg_enable;
    logic [INSTRUMENT_W-1:0] cfg_instrument;
    logic [PRICE_W-1:0] cfg_min_price, cfg_max_price;
    logic [QTY_W-1:0] cfg_max_qty;
    logic [NOTIONAL_W-1:0] cfg_max_order_notional, cfg_max_buy_gross, cfg_max_sell_gross;
    logic s_valid, s_ready, s_sell;
    logic [TAG_W-1:0] s_tag;
    logic [INSTRUMENT_W-1:0] s_instrument;
    logic [PRICE_W-1:0] s_price;
    logic [QTY_W-1:0] s_qty;
    logic m_valid, m_ready, m_sell, m_allow;
    logic [TAG_W-1:0] m_tag;
    logic [INSTRUMENT_W-1:0] m_instrument;
    logic [PRICE_W-1:0] m_price;
    logic [QTY_W-1:0] m_qty;
    logic [7:0] m_reject;
    logic [NOTIONAL_W-1:0] m_notional, m_gross_after;

    pretrade_risk_gateway #(
        .NUM_INSTRUMENTS(NUM_INSTRUMENTS), .PRICE_W(PRICE_W), .QTY_W(QTY_W), .TAG_W(TAG_W)
    ) dut (.*);

    typedef struct packed {
        logic [TAG_W-1:0] tag;
        logic [INSTRUMENT_W-1:0] instrument;
        logic sell;
        logic [PRICE_W-1:0] price;
        logic [QTY_W-1:0] qty;
    } payload_t;

    typedef struct packed {
        payload_t payload;
        logic allow;
        logic [7:0] reject;
        logic [NOTIONAL_W-1:0] notional;
        logic [NOTIONAL_W-1:0] gross_after;
    } response_t;

    payload_t pending [0:QUEUE_DEPTH-1];
    payload_t request;
    response_t expected, observed;
    integer read_index, write_index;
    bit policy_active [0:NUM_INSTRUMENTS-1];
    integer unsigned minimum_price [0:NUM_INSTRUMENTS-1];
    integer unsigned maximum_price [0:NUM_INSTRUMENTS-1];
    integer unsigned maximum_qty [0:NUM_INSTRUMENTS-1];
    integer unsigned maximum_order [0:NUM_INSTRUMENTS-1];
    integer unsigned maximum_gross [0:NUM_INSTRUMENTS-1][0:1];
    integer unsigned gross [0:NUM_INSTRUMENTS-1][0:1];
    integer unsigned product, before_gross, candidate_gross;
    integer instrument, direction;
    integer j;
    bit prior_stall, halt_at_commit;
    integer allowed_count = 0;
    integer rejected_count = 0;
    integer stalled_count = 0;
    logic [7:0] observed_flags = '0;

    // Model full integer sums against policy ceilings. The DUT uses a credit ledger.
    // An output first appears one sample after commit, even when it cannot transfer.
    always @(posedge clk) begin
        if (rst) begin
            if (s_ready !== 0 || m_valid !== 0 || cfg_ready !== 0)
                $fatal(1, "handshake visible during reset");
            read_index = 0;
            write_index = 0;
            prior_stall = 0;
            halt_at_commit = 0;
            for (j = 0; j < NUM_INSTRUMENTS; j = j + 1) begin
                policy_active[j] = 0;
                gross[j][0] = 0;
                gross[j][1] = 0;
            end
        end else begin
            if (prior_stall && !m_valid) $fatal(1, "held output disappeared");
            if (m_valid) begin
                if (read_index == write_index) $fatal(1, "unexpected output tag %0d", m_tag);
                if (!prior_stall) begin
                    request = pending[read_index];
                    instrument = int'(request.instrument);
                    direction = int'(request.sell);
                    product = int'(request.price) * int'(request.qty);
                    before_gross = 0;
                    expected = '0;
                    expected.payload = request;
                    expected.notional = NOTIONAL_W'(product);
                    expected.reject[2] = request.qty == 0;
                    expected.reject[7] = halt_at_commit;
                    if (instrument >= NUM_INSTRUMENTS) begin
                        expected.reject[0] = 1;
                    end else begin
                        before_gross = gross[instrument][direction];
                        if (!policy_active[instrument]) begin
                            expected.reject[1] = 1;
                        end else begin
                            candidate_gross = before_gross + product;
                            expected.reject[3] = request.qty > maximum_qty[instrument];
                            expected.reject[4] = request.price < minimum_price[instrument] ||
                                                 request.price > maximum_price[instrument];
                            expected.reject[5] = product > maximum_order[instrument];
                            // A zero notional fits zero available credit. It fails an
                            // independent nonzero quantity or positive-price check.
                            expected.reject[6] = product != 0 &&
                                                 candidate_gross > maximum_gross[instrument][direction];
                        end
                    end
                    expected.allow = expected.reject == 0;
                    if (expected.allow) begin
                        gross[instrument][direction] = before_gross + product;
                        before_gross = gross[instrument][direction];
                    end
                    expected.gross_after = NOTIONAL_W'(before_gross);
                end
                observed = {m_tag, m_instrument, m_sell, m_price, m_qty, m_allow,
                            m_reject, m_notional, m_gross_after};
                if (observed !== expected)
                    $fatal(1, "tag %0d expected allow=%b reject=%h product=%0d gross=%0d; got tag=%0d allow=%b reject=%h product=%0d gross=%0d",
                           expected.payload.tag, expected.allow, expected.reject,
                           expected.notional, expected.gross_after, m_tag, m_allow,
                           m_reject, m_notional, m_gross_after);
                if (m_ready) begin
                    if (m_allow) allowed_count = allowed_count + 1;
                    else rejected_count = rejected_count + 1;
                    observed_flags = observed_flags | m_reject;
                    read_index = read_index + 1;
                end else begin
                    stalled_count = stalled_count + 1;
                end
            end
            prior_stall = m_valid && !m_ready;
            halt_at_commit = halt_at_commit || halt_req;

            if (cfg_valid && cfg_ready) begin
                if (read_index != write_index || s_ready)
                    $fatal(1, "configuration overlapped work");
                if (int'(cfg_instrument) < NUM_INSTRUMENTS) begin
                    policy_active[cfg_instrument] = cfg_enable && cfg_min_price != 0 &&
                        cfg_min_price <= cfg_max_price && cfg_max_qty != 0 && cfg_max_order_notional != 0;
                    minimum_price[cfg_instrument] = int'(cfg_min_price);
                    maximum_price[cfg_instrument] = int'(cfg_max_price);
                    maximum_qty[cfg_instrument] = int'(cfg_max_qty);
                    maximum_order[cfg_instrument] = int'(cfg_max_order_notional);
                    maximum_gross[cfg_instrument][0] = int'(cfg_max_buy_gross);
                    maximum_gross[cfg_instrument][1] = int'(cfg_max_sell_gross);
                end
            end
            if (s_valid && s_ready) begin
                if (write_index == QUEUE_DEPTH) $fatal(1, "scoreboard capacity exceeded");
                pending[write_index] = {s_tag, s_instrument, s_sell, s_price, s_qty};
                write_index = write_index + 1;
            end
        end
    end

    task automatic drive_order(input integer tag, instrument, sell, price, qty);
        s_valid = 1;
        s_tag = TAG_W'(tag);
        s_instrument = INSTRUMENT_W'(instrument);
        s_sell = 1'(sell);
        s_price = PRICE_W'(price);
        s_qty = QTY_W'(qty);
    endtask

    task automatic submit(input integer tag, instrument, sell, price, qty);
        @(negedge clk);
        drive_order(tag, instrument, sell, price, qty);
        do @(posedge clk); while (!s_ready);
    endtask

    task automatic drive_config(input integer instrument, active, lo, hi, qty, order_max, buy_max, sell_max);
        cfg_valid = 1;
        cfg_instrument = INSTRUMENT_W'(instrument);
        cfg_enable = 1'(active);
        cfg_min_price = PRICE_W'(lo);
        cfg_max_price = PRICE_W'(hi);
        cfg_max_qty = QTY_W'(qty);
        cfg_max_order_notional = NOTIONAL_W'(order_max);
        cfg_max_buy_gross = NOTIONAL_W'(buy_max);
        cfg_max_sell_gross = NOTIONAL_W'(sell_max);
    endtask

    task automatic configure(input integer instrument, active, lo, hi, qty, order_max, buy_max, sell_max);
        @(negedge clk);
        s_valid = 0;
        drive_config(instrument, active, lo, hi, qty, order_max, buy_max, sell_max);
        do @(posedge clk); while (!cfg_ready);
        @(negedge clk);
        cfg_valid = 0;
    endtask

    task automatic drain;
        @(negedge clk);
        s_valid = 0;
        m_ready = 1;
        repeat (5) @(negedge clk);
        if (read_index != write_index || m_valid)
            $fatal(1, "drain failed (%0d decisions for %0d requests)", read_index, write_index);
    endtask

    function automatic logic [31:0] next_random(input logic [31:0] seed);
        logic [31:0] state;
        state = seed ^ (seed << 13);
        state = state ^ (state >> 17);
        return state ^ (state << 5);
    endfunction

    logic [31:0] random_state = 32'h4d59_5df4;
    bit retry_order;
    integer n, random_tag;
    initial begin
        rst = 1;
        halt_req = 0;
        cfg_valid = 0;
        s_valid = 0;
        m_ready = 1;
        repeat (2) @(negedge clk);
        rst = 0;

        submit(1, 0, 0, 10, 1);  // Unconfigured.
        submit(2, 3, 0, 10, 0);  // Out-of-range ID plus zero quantity.
        drain();
        configure(0, 1, 10, 30, 4, 75, 150, 50);
        configure(1, 1, 1, 255, 255, 65535, 65535, 0);
        configure(2, 1, 50, 40, 10, 1000, 10000, 10000);  // Malformed collar.
        configure(3, 1, 1, 255, 255, 65535, 65535, 65535); // Invalid address.

        submit(3, 0, 0, 10, 2);
        @(negedge clk);
        s_valid = 0;
        if (m_valid) $fatal(1, "order appeared after capture edge");
        @(negedge clk);
        if (m_valid) $fatal(1, "order appeared after multiply edge");
        @(negedge clk);
        if (!m_valid || m_tag != 3 || !m_allow) $fatal(1, "order missing after decision edge");
        submit(4, 0, 0, 25, 3);  // Hits exact notional limit.
        submit(5, 0, 0, 11, 5);  // Quantity fails.
        submit(6, 0, 0, 0, 1);   // Price fails.
        submit(7, 0, 0, 30, 3);  // Notional and side budget both fail.
        submit(8, 0, 1, 25, 2);  // Independent sell budget reaches exact limit.
        submit(9, 0, 1, 10, 1);  // Gross cap alone fails.
        submit(10, 0, 0, 20, 2); // Buy side still has credit.
        submit(11, 2, 0, 10, 1); // Invalid policy disabled the instrument.
        submit(12, 1, 1, 1, 1);  // Sell side disabled with a zero credit cap.
        submit(13, 1, 0, 255, 255); // Product is 65,025 with upper bits set.
        submit(14, 1, 0, 255, 3);   // A wrapping 16-bit cumulative sum would pass.
        submit(15, 1, 0, 255, 2);   // Exactly 65,535.
        submit(16, 1, 0, 1, 1);
        drain();

        // Three entries fit while egress is blocked, including repeated reservations.
        configure(2, 1, 1, 100, 100, 1000, 20, 20);
        @(negedge clk);
        m_ready = 0;
        submit(20, 2, 0, 5, 1);
        submit(21, 2, 0, 5, 2);
        submit(22, 2, 0, 5, 1);
        @(negedge clk);
        drive_order(23, 2, 0, 5, 1);
        repeat (4) begin
            @(posedge clk);
            if (s_ready || !m_valid || m_tag != 20) $fatal(1, "full pipeline stalled incorrectly");
        end
        @(negedge clk);
        m_ready = 1;
        @(posedge clk);
        if (!s_ready) $fatal(1, "pipeline failed to resume in the release cycle");
        drain();

        // A write waits for a pending response and blocks pending order intake.
        @(negedge clk);
        m_ready = 0;
        submit(30, 2, 1, 5, 1);
        @(negedge clk);
        drive_order(31, 2, 0, 5, 1);
        drive_config(2, 1, 1, 100, 100, 1000, 10, 20);
        repeat (5) begin
            @(posedge clk);
            if (cfg_ready || s_ready) $fatal(1, "configuration did not wait for held response");
        end
        @(negedge clk);
        m_ready = 1;
        do @(posedge clk); while (!cfg_ready);
        @(negedge clk);
        cfg_valid = 0;
        do @(posedge clk); while (!s_ready);
        drain();
        configure(2, 1, 1, 100, 100, 1000, 25, 20);
        submit(32, 2, 0, 5, 1); // Exactly the restored credit of five.
        submit(33, 2, 0, 5, 1);
        drain();
        configure(2, 0, 1, 100, 100, 1000, 10000, 10000);
        submit(34, 2, 0, 1, 1);
        drain();

        configure(0, 1, 4, 45, 12, 200, 12000, 9500);
        configure(1, 1, 10, 50, 8, 250, 20000, 8500);
        configure(2, 1, 2, 40, 22, 400, 9000, 11000);
        retry_order = 0;
        random_tag = 100;
        for (n = 0; n < 2400; n = n + 1) begin
            @(negedge clk);
            random_state = next_random(random_state);
            m_ready = random_state[2:0] < 5;
            if (!retry_order) begin
                if (random_state[5:3] != 0) begin
                    drive_order(random_tag, int'(random_state[7:6]), int'(random_state[8]),
                                int'(random_state[14:9]), int'(random_state[19:15]));
                    random_tag = random_tag + 1;
                end else s_valid = 0;
            end
            @(posedge clk);
            retry_order = s_valid && !s_ready;
        end
        if (retry_order) begin
            @(negedge clk);
            m_ready = 1;
            do @(posedge clk); while (!s_ready);
        end
        drain();

        // The committed response stays immutable while halt blocks later decisions.
        configure(2, 1, 1, 100, 100, 1000, 65535, 65535);
        @(negedge clk);
        m_ready = 0;
        submit(3000, 2, 0, 1, 1);
        submit(3001, 2, 0, 1, 1);
        submit(3002, 2, 0, 1, 1);
        @(negedge clk);
        s_valid = 0;
        if (!m_valid || !m_allow || m_tag != 3000) $fatal(1, "halt setup failed");
        halt_req = 1;
        @(negedge clk);
        halt_req = 0;
        if (!halted) $fatal(1, "single-cycle halt failed to latch");
        m_ready = 1;
        drain();
        submit(3003, 2, 0, 1, 1); // Still halted after the pulse.
        drain();

        // A request asserted on the actual decision edge blocks that decision.
        @(negedge clk);
        rst = 1;
        @(negedge clk);
        rst = 0;
        configure(2, 1, 1, 100, 100, 1000, 100, 100);
        submit(3004, 2, 0, 1, 1);
        @(negedge clk);
        s_valid = 0;
        @(negedge clk);
        halt_req = 1;
        @(negedge clk);
        if (!m_valid || m_allow || m_reject != 8'h80) $fatal(1, "halt missed the decision edge");
        halt_req = 0;
        drain();

        // Reset can flush captured requests and a stalled decision mid-session.
        @(negedge clk);
        m_ready = 0;
        submit(3010, 2, 0, 1, 1);
        submit(3011, 2, 0, 1, 1);
        submit(3012, 2, 0, 1, 1);
        @(negedge clk);
        rst = 1;
        s_valid = 0;
        @(negedge clk);
        rst = 0;
        m_ready = 1;
        @(negedge clk);
        if (halted || m_valid) $fatal(1, "reset failed to flush committed or queued work");
        submit(3013, 2, 0, 1, 1); // Reset disarmed the policy.
        drain();
        configure(2, 1, 1, 100, 100, 1000, 1, 1);
        submit(3014, 2, 0, 1, 1); // First post-reset order can use the full new budget.
        drain();

        if (observed_flags != 8'hff || allowed_count < 30 || rejected_count < 100 || stalled_count < 100)
            $fatal(1, "insufficient coverage: flags=%h allow=%0d reject=%0d stalls=%0d",
                   observed_flags, allowed_count, rejected_count, stalled_count);
        $display("PASS risk gateway: %0d decisions (%0d allowed, %0d rejected), %0d stalled output cycles, flags=%h",
                 allowed_count + rejected_count, allowed_count, rejected_count, stalled_count, observed_flags);
        $finish;
    end

    initial begin
        #100000;
        $fatal(1, "testbench watchdog expired");
    end
endmodule

`default_nettype wire
