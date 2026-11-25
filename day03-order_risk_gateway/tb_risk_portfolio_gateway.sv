`timescale 1ns/1ps
`default_nettype none

module tb_risk_portfolio_gateway #(
    parameter integer NUM_ACCOUNTS = 3,
    parameter integer NUM_INSTRUMENTS = 3,
    parameter integer ORDER_SLOTS = 5,
    parameter integer PRICE_W = 8,
    parameter integer QTY_W = 8,
    parameter integer GEN_W = 4,
    parameter integer EVENT_W = 5
);
    localparam integer TAG_W = 16;
    localparam integer ACCOUNT_W = NUM_ACCOUNTS > 1 ? $clog2(NUM_ACCOUNTS) : 1;
    localparam integer INSTRUMENT_W = NUM_INSTRUMENTS > 1 ? $clog2(NUM_INSTRUMENTS) : 1;
    localparam integer SLOT_W = ORDER_SLOTS > 1 ? $clog2(ORDER_SLOTS) : 1;
    localparam integer COUNT_W = $clog2(ORDER_SLOTS + 1);
    localparam integer AMOUNT_W = PRICE_W + QTY_W;
    localparam integer TOTAL_W = AMOUNT_W + $clog2(ORDER_SLOTS + 2*NUM_ACCOUNTS*NUM_INSTRUMENTS + 1);
    localparam integer NATIVE_W = 3 + TAG_W + ACCOUNT_W + INSTRUMENT_W + 1 + SLOT_W +
                        GEN_W + EVENT_W + 5 + 1 + QTY_W + PRICE_W + 3*AMOUNT_W + COUNT_W;
    localparam integer DATA_W = NATIVE_W + 3 + 4*TOTAL_W + 2*COUNT_W + 32;
    localparam integer CTL_W = 6 + 4*TOTAL_W + 2*COUNT_W + 32;
    localparam integer NATIVE_STATUS_LSB = 1 + QTY_W + PRICE_W + 3*AMOUNT_W + COUNT_W;

    logic clk = 0;
    always #5 clk = !clk;

    logic rst = 1;
    logic halt_req = 0;
    wire halted;

    logic cfg_valid = 0, cfg_sell, cfg_enable;
    wire cfg_ready;
    logic [ACCOUNT_W-1:0] cfg_account;
    logic [INSTRUMENT_W-1:0] cfg_instrument;
    logic [PRICE_W-1:0] cfg_min_price, cfg_max_price;
    logic [QTY_W-1:0] cfg_max_order_qty;
    logic [AMOUNT_W-1:0] cfg_max_open_notional;
    logic [COUNT_W-1:0] cfg_max_open_orders;

    logic s_valid = 0, s_sell;
    wire s_ready;
    logic [2:0] s_op;
    logic [TAG_W-1:0] s_tag;
    logic [ACCOUNT_W-1:0] s_account;
    logic [INSTRUMENT_W-1:0] s_instrument;
    logic [SLOT_W-1:0] s_slot;
    logic [GEN_W-1:0] s_generation;
    logic [EVENT_W-1:0] s_event_seq;
    logic [PRICE_W-1:0] s_price;
    logic [QTY_W-1:0] s_qty;

    wire m_valid;
    logic m_ready = 0;
    wire [NATIVE_W-1:0] m_native;
    wire [2:0] m_portfolio_reason;
    wire [TOTAL_W-1:0] m_account_gross, m_account_turnover, m_firm_gross, m_firm_turnover;
    wire [COUNT_W-1:0] m_account_orders, m_firm_orders;
    wire [31:0] m_epoch;

    logic ctl_valid = 0, ctl_firm;
    wire ctl_ready;
    logic [1:0] ctl_op;
    logic [ACCOUNT_W-1:0] ctl_account;
    logic [2:0] ctl_mask;
    logic [TOTAL_W-1:0] ctl_gross_limit, ctl_usage_limit;
    logic [COUNT_W-1:0] ctl_count_limit;
    logic [31:0] ctl_epoch;
    wire ctl_m_valid;
    logic ctl_m_ready = 0;
    wire [2:0] ctl_m_status, ctl_m_mask;
    wire [TOTAL_W-1:0] ctl_m_gross_limit, ctl_m_usage_limit, ctl_m_gross, ctl_m_turnover;
    wire [COUNT_W-1:0] ctl_m_count_limit, ctl_m_orders;
    wire [31:0] ctl_m_epoch;

    risk_portfolio_gateway #(
        .NUM_ACCOUNTS(NUM_ACCOUNTS), .NUM_INSTRUMENTS(NUM_INSTRUMENTS), .ORDER_SLOTS(ORDER_SLOTS),
        .TAG_W(TAG_W), .PRICE_W(PRICE_W), .QTY_W(QTY_W), .GEN_W(GEN_W), .EVENT_W(EVENT_W)
    ) dut (.*);

    wire [DATA_W-1:0] data_reply = {
        m_native, m_portfolio_reason, m_account_gross, m_account_turnover,
        m_account_orders, m_firm_gross, m_firm_turnover, m_firm_orders, m_epoch
    };
    wire [CTL_W-1:0] ctl_reply = {
        ctl_m_status, ctl_m_mask, ctl_m_gross_limit, ctl_m_count_limit, ctl_m_usage_limit,
        ctl_m_gross, ctl_m_turnover, ctl_m_orders, ctl_m_epoch
    };

    reg data_stalled = 0, ctl_stalled = 0;
    reg [DATA_W-1:0] data_hold;
    reg [CTL_W-1:0] ctl_hold;
    integer checked_data_stalls = 0, checked_ctl_stalls = 0;

    always @(posedge clk) begin
        if (rst) begin
            data_stalled <= 0;
            ctl_stalled <= 0;
        end else begin
            if (data_stalled && (!m_valid || data_reply !== data_hold))
                $fatal(1, "held data response changed");
            if (ctl_stalled && (!ctl_m_valid || ctl_reply !== ctl_hold))
                $fatal(1, "held control response changed");
            if ((m_valid || ctl_m_valid) && (s_ready || cfg_ready || ctl_ready))
                $fatal(1, "another command overtook a held response");
            if (m_valid && ctl_m_valid)
                $fatal(1, "mutually exclusive response channels were both valid");
            data_stalled <= m_valid && !m_ready;
            ctl_stalled <= ctl_m_valid && !ctl_m_ready;
            if (m_valid && !m_ready) begin
                data_hold <= data_reply;
                checked_data_stalls <= checked_data_stalls + 1;
            end
            if (ctl_m_valid && !ctl_m_ready) begin
                ctl_hold <= ctl_reply;
                checked_ctl_stalls <= checked_ctl_stalls + 1;
            end
        end
    end

    integer stimulus, capture, row_id, act, fields;
    integer data_replies = 0, ctl_replies = 0;
    reg [127:0] row [0:9];
    reg [4095:0] vector_dir, stimulus_path, capture_path, wave_path;

    // Every task exits on a falling edge with its channel inactive.
    task automatic configure;
        begin
            {cfg_account,cfg_instrument,cfg_sell,cfg_enable} =
                         {ACCOUNT_W'(row[0]),INSTRUMENT_W'(row[1]),1'(row[2]),1'(row[3])};
            cfg_min_price = PRICE_W'(row[4]);
            cfg_max_price = PRICE_W'(row[5]);
            cfg_max_order_qty = QTY_W'(row[6]);
            cfg_max_open_notional = AMOUNT_W'(row[7]);
            cfg_max_open_orders = COUNT_W'(row[8]);
            cfg_valid = 1;
            do @(posedge clk); while (!cfg_ready);
            @(negedge clk);
            cfg_valid = 0;
        end
    endtask

    task automatic order;
        begin
            s_op = 3'(row[0]);
            s_tag = TAG_W'(row[1]);
            s_account = ACCOUNT_W'(row[2]);
            s_instrument = INSTRUMENT_W'(row[3]);
            s_sell = 1'(row[4]);
            s_slot = SLOT_W'(row[5]);
            s_generation = GEN_W'(row[6]);
            s_event_seq = EVENT_W'(row[7]);
            s_price = PRICE_W'(row[8]);
            s_qty = QTY_W'(row[9]);
            s_valid = 1;
            do @(posedge clk); while (!s_ready);
            @(negedge clk);
            s_valid = 0;
            while (!m_valid) @(negedge clk);
            repeat (row_id % 6) @(negedge clk);
            m_ready = 1;
            @(posedge clk);
            if (!m_valid) $fatal(1, "expected data response");
            $fdisplay(capture, "%0h 2 %0h %0h %0h %0h %0h %0h %0h %0h %0h",
                      row_id, m_native, m_portfolio_reason, m_account_gross, m_account_turnover,
                      m_account_orders, m_firm_gross, m_firm_turnover, m_firm_orders, m_epoch);
            data_replies = data_replies + 1;
            @(negedge clk);
            m_ready = 0;
        end
    endtask

    task automatic control;
        begin
            ctl_op = 2'(row[0]);
            ctl_firm = 1'(row[1]);
            ctl_account = ACCOUNT_W'(row[2]);
            ctl_mask = 3'(row[3]);
            ctl_gross_limit = TOTAL_W'(row[4]);
            ctl_count_limit = COUNT_W'(row[5]);
            ctl_usage_limit = TOTAL_W'(row[6]);
            ctl_epoch = 32'(row[7]);
            ctl_valid = 1;
            do @(posedge clk); while (!ctl_ready);
            @(negedge clk);
            ctl_valid = 0;
            while (!ctl_m_valid) @(negedge clk);
            repeat ((row_id * 3) % 7) @(negedge clk);
            ctl_m_ready = 1;
            @(posedge clk);
            if (!ctl_m_valid) $fatal(1, "expected control response");
            $fdisplay(capture, "%0h 3 %0h %0h %0h %0h %0h %0h %0h %0h %0h",
                      row_id, ctl_m_status, ctl_m_mask, ctl_m_gross_limit, ctl_m_count_limit,
                      ctl_m_usage_limit, ctl_m_gross, ctl_m_turnover, ctl_m_orders, ctl_m_epoch);
            ctl_replies = ctl_replies + 1;
            @(negedge clk);
            ctl_m_ready = 0;
        end
    endtask

    // Exercise simultaneous input priorities and a halt after a cap denial
    // has been computed, before its read-only snapshot returns.
    task automatic simultaneous_and_late_halt;
        begin
            rst = 1;
            @(negedge clk);
            rst = 0;
            cfg_valid = 1;
            cfg_account = 0;
            cfg_instrument = 0;
            cfg_sell = 0;
            cfg_enable = 1;
            cfg_min_price = 1;
            cfg_max_price = '1;
            cfg_max_order_qty = '1;
            cfg_max_open_notional = '1;
            cfg_max_open_orders = COUNT_W'(ORDER_SLOTS);
            ctl_valid = 1;
            ctl_op = 0;
            ctl_firm = 0;
            ctl_account = 0;
            ctl_mask = 1;
            ctl_gross_limit = 0;
            ctl_count_limit = 0;
            ctl_usage_limit = 0;
            ctl_epoch = 0;
            s_valid = 1;
            s_op = 0;
            s_tag = 16'h68;
            s_account = 0;
            s_instrument = 0;
            s_sell = 0;
            s_slot = 0;
            s_generation = 0;
            s_event_seq = 0;
            s_price = 10;
            s_qty = 6;
            #1;
            if (!cfg_ready || ctl_ready || s_ready) $fatal(1, "native policy priority violated");
            @(negedge clk);
            cfg_valid = 0;
            #1;
            if (!ctl_ready || s_ready) $fatal(1, "portfolio control priority violated");
            @(negedge clk);
            ctl_valid = 0;
            if (!ctl_m_valid || ctl_m_status != 0 || ctl_m_mask != 1 || s_ready)
                $fatal(1, "cap command not committed before the queued order");
            ctl_m_ready = 1;
            @(negedge clk);
            ctl_m_ready = 0;
            do @(posedge clk); while (!s_ready);
            @(negedge clk);
            s_valid = 0;
            repeat (3) @(negedge clk);
            halt_req = 1;
            @(negedge clk);
            halt_req = 0;
            while (!m_valid) @(negedge clk);
            if (m_native[NATIVE_STATUS_LSB+:5] != 5'd19 || m_portfolio_reason != 0 ||
                m_account_gross != 0 || m_firm_gross != 0 || m_firm_orders != 0)
                $fatal(1, "late halt did not preserve zero exposure");
            m_ready = 1;
            @(negedge clk);
            m_ready = 0;
            $display("PASS: command priority and late-halt denial race");
        end
    endtask

    initial begin
        if (!$value$plusargs("vector_dir=%s",vector_dir))
            $fatal(1, "missing +vector_dir");
        $sformat(stimulus_path, "%0s/stimulus.hex", vector_dir);
        $sformat(capture_path, "%0s/actual.hex", vector_dir);
        stimulus = $fopen(stimulus_path,"r");
        capture = $fopen(capture_path,"w");
        if (!stimulus || !capture)
            $fatal(1, "cannot open stimulus or capture");
        if ($value$plusargs("wave=%s",wave_path)) begin
            $dumpfile(wave_path);
            $dumpvars(1,tb_risk_portfolio_gateway);
        end

        repeat (2) @(negedge clk);
        rst = 0;
        row_id = 0;
        while (!$feof(stimulus)) begin
            fields = $fscanf(stimulus,"%h %h %h %h %h %h %h %h %h %h %h",
                             act, row[0], row[1], row[2], row[3], row[4], row[5],
                             row[6], row[7], row[8], row[9]);
            if (fields == 11) begin
                row_id = row_id + 1;
                case (act)
                    0: begin
                        rst = 1;
                        @(negedge clk);
                        rst = 0;
                    end
                    1: configure();
                    2: order();
                    3: control();
                    4: begin
                        halt_req = 1;
                        @(negedge clk);
                        halt_req = 0;
                        #1;
                        if (!halted) $fatal(1, "halt did not latch");
                        @(negedge clk);
                    end
                    default: $fatal(1, "unknown action %0d", act);
                endcase
            end else if (fields > 0 || !$feof(stimulus))
                $fatal(1, "malformed vector row %0d", row_id+1);
        end
        $fclose(stimulus);
        $fclose(capture);
        if (data_replies == 0 || ctl_replies == 0 || checked_data_stalls == 0 || checked_ctl_stalls == 0)
            $fatal(1, "missing coverage data %0d control %0d stalled %0d/%0d",
                   data_replies, ctl_replies, checked_data_stalls, checked_ctl_stalls);
        $display("PASS: %0d order outcomes and %0d control responses with %0d/%0d held cycles",
                 data_replies, ctl_replies, checked_data_stalls, checked_ctl_stalls);
        simultaneous_and_late_halt();
        $finish;
    end

    initial begin
        #100000000;
        $fatal(1, "watchdog expired");
    end
endmodule

`default_nettype wire
