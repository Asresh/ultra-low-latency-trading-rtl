`timescale 1ns/1ps
`default_nettype none

module pretrade_risk_gateway #(
    parameter integer NUM_INSTRUMENTS = 4,
    parameter integer PRICE_W = 32,
    parameter integer QTY_W = 24,
    parameter integer TAG_W = 16,
    localparam integer INSTRUMENT_W = NUM_INSTRUMENTS > 1 ? $clog2(NUM_INSTRUMENTS) : 1,
    localparam integer NOTIONAL_W = PRICE_W + QTY_W
) (
    input  wire                    clk,
    input  wire                    rst,

    input  wire                    halt_req,
    output wire                    halted,

    input  wire                    cfg_valid,
    output wire                    cfg_ready,
    input  wire [INSTRUMENT_W-1:0] cfg_instrument,
    input  wire                    cfg_enable,
    input  wire [PRICE_W-1:0]      cfg_min_price,
    input  wire [PRICE_W-1:0]      cfg_max_price,
    input  wire [QTY_W-1:0]        cfg_max_qty,
    input  wire [NOTIONAL_W-1:0]   cfg_max_order_notional,
    input  wire [NOTIONAL_W-1:0]   cfg_max_buy_gross,
    input  wire [NOTIONAL_W-1:0]   cfg_max_sell_gross,

    input  wire                    s_valid,
    output wire                    s_ready,
    input  wire [TAG_W-1:0]        s_tag,
    input  wire [INSTRUMENT_W-1:0] s_instrument,
    input  wire                    s_sell,
    input  wire [PRICE_W-1:0]      s_price,
    input  wire [QTY_W-1:0]        s_qty,

    output wire                    m_valid,
    input  wire                    m_ready,
    output wire [TAG_W-1:0]        m_tag,
    output wire [INSTRUMENT_W-1:0] m_instrument,
    output wire                    m_sell,
    output wire [PRICE_W-1:0]      m_price,
    output wire [QTY_W-1:0]        m_qty,
    output wire                    m_allow,
    output wire [7:0]              m_reject,
    output wire [NOTIONAL_W-1:0]   m_notional,
    output wire [NOTIONAL_W-1:0]   m_gross_after
);
    localparam [INSTRUMENT_W:0] INSTRUMENT_COUNT = (INSTRUMENT_W+1)'(NUM_INSTRUMENTS);

    localparam integer BAD_INSTRUMENT = 0;
    localparam integer DISABLED = 1;
    localparam integer ZERO_QTY = 2;
    localparam integer QTY_LIMIT = 3;
    localparam integer PRICE_COLLAR = 4;
    localparam integer ORDER_NOTIONAL_LIMIT = 5;
    localparam integer SIDE_GROSS_LIMIT = 6;
    localparam integer HALTED = 7;

    typedef struct packed {
        logic [TAG_W-1:0] tag;
        logic [INSTRUMENT_W-1:0] instrument;
        logic sell;
        logic [PRICE_W-1:0] price;
        logic [QTY_W-1:0] qty;
    } order_t;

    typedef struct packed {
        order_t order;
        logic in_range;
        logic enabled;
        logic [PRICE_W-1:0] min_price;
        logic [PRICE_W-1:0] max_price;
        logic [QTY_W-1:0] max_qty;
        logic [NOTIONAL_W-1:0] max_order_notional;
    } capture_t;

    typedef struct packed {
        order_t order;
        logic in_range;
        logic enabled;
        logic [NOTIONAL_W-1:0] max_order_notional;
        logic [NOTIONAL_W-1:0] notional;
        logic [7:0] reject;
    } compute_t;

    typedef struct packed {
        order_t order;
        logic [7:0] reject;
        logic [NOTIONAL_W-1:0] notional;
        logic [NOTIONAL_W-1:0] gross_after;
    } decision_t;

    logic [NUM_INSTRUMENTS-1:0] enabled;
    logic [PRICE_W-1:0] min_price [0:NUM_INSTRUMENTS-1];
    logic [PRICE_W-1:0] max_price [0:NUM_INSTRUMENTS-1];
    logic [QTY_W-1:0] max_qty [0:NUM_INSTRUMENTS-1];
    logic [NOTIONAL_W-1:0] max_order_notional [0:NUM_INSTRUMENTS-1];
    logic [NOTIONAL_W-1:0] buy_gross [0:NUM_INSTRUMENTS-1];
    logic [NOTIONAL_W-1:0] sell_gross [0:NUM_INSTRUMENTS-1];
    logic [NOTIONAL_W-1:0] buy_credit [0:NUM_INSTRUMENTS-1];
    logic [NOTIONAL_W-1:0] sell_credit [0:NUM_INSTRUMENTS-1];

    capture_t capture_q, capture_d;
    compute_t compute_q, compute_d;
    decision_t decision_q, decision_d;

    logic capture_valid, compute_valid, decision_valid;
    logic halt_latched;
    logic [NOTIONAL_W-1:0] prior_credit, prior_gross, credit_after;
    logic [NOTIONAL_W-1:0] accepted_gross;
    wire decision_ready = !decision_valid || m_ready;
    wire compute_ready = !compute_valid || decision_ready;
    wire capture_ready = !capture_valid || compute_ready;
    wire reserve = compute_valid && decision_ready && decision_d.reject == 8'b0;
    wire cfg_in_range = {1'b0, cfg_instrument} < INSTRUMENT_COUNT;
    wire cfg_limits_ok = cfg_min_price != '0 && cfg_min_price <= cfg_max_price &&
                         cfg_max_qty != '0 && cfg_max_order_notional != '0;

    assign halted = halt_latched || halt_req;
    assign cfg_ready = !rst && !capture_valid && !compute_valid && !decision_valid;
    assign s_ready = !rst && !cfg_valid && capture_ready;
    assign m_valid = !rst && decision_valid;
    assign m_tag = decision_q.order.tag;
    assign m_instrument = decision_q.order.instrument;
    assign m_sell = decision_q.order.sell;
    assign m_price = decision_q.order.price;
    assign m_qty = decision_q.order.qty;
    assign m_allow = decision_q.reject == 8'b0;
    assign m_reject = decision_q.reject;
    assign m_notional = decision_q.notional;
    assign m_gross_after = decision_q.gross_after;

    // Stage 1 takes a snapshot of the static policy associated with the order.
    always @* begin
        capture_d = '0;
        capture_d.order.tag = s_tag;
        capture_d.order.instrument = s_instrument;
        capture_d.order.sell = s_sell;
        capture_d.order.price = s_price;
        capture_d.order.qty = s_qty;
        capture_d.in_range = {1'b0, s_instrument} < INSTRUMENT_COUNT;
        if (capture_d.in_range && enabled[s_instrument]) begin
            capture_d.enabled = 1'b1;
            capture_d.min_price = min_price[s_instrument];
            capture_d.max_price = max_price[s_instrument];
            capture_d.max_qty = max_qty[s_instrument];
            capture_d.max_order_notional = max_order_notional[s_instrument];
        end
    end

    // The destination is the sum of operand widths, so the unsigned product is exact.
    always @* begin
        compute_d = '0;
        compute_d.order = capture_q.order;
        compute_d.in_range = capture_q.in_range;
        compute_d.enabled = capture_q.enabled;
        compute_d.notional = capture_q.order.price * capture_q.order.qty;
        compute_d.max_order_notional = capture_q.max_order_notional;
        compute_d.reject[BAD_INSTRUMENT] = !capture_q.in_range;
        compute_d.reject[DISABLED] = capture_q.in_range && !capture_q.enabled;
        compute_d.reject[ZERO_QTY] = capture_q.order.qty == '0;
        if (capture_q.enabled) begin
            compute_d.reject[QTY_LIMIT] = capture_q.order.qty > capture_q.max_qty;
            compute_d.reject[PRICE_COLLAR] = capture_q.order.price < capture_q.min_price ||
                                              capture_q.order.price > capture_q.max_price;
        end
    end

    // Read current credit at commit, after all preceding decisions have reserved theirs.
    // Keeping credit and gross separately puts subtraction in parallel with comparison.
    always @* begin
        prior_credit = '0;
        prior_gross = '0;
        if (compute_q.in_range) begin
            if (compute_q.order.sell) begin
                prior_credit = sell_credit[compute_q.order.instrument];
                prior_gross = sell_gross[compute_q.order.instrument];
            end else begin
                prior_credit = buy_credit[compute_q.order.instrument];
                prior_gross = buy_gross[compute_q.order.instrument];
            end
        end

        decision_d = '0;
        decision_d.order = compute_q.order;
        decision_d.notional = compute_q.notional;
        decision_d.reject = compute_q.reject;
        decision_d.reject[HALTED] = halted;
        if (compute_q.enabled) begin
            decision_d.reject[ORDER_NOTIONAL_LIMIT] = compute_q.notional > compute_q.max_order_notional;
            decision_d.reject[SIDE_GROSS_LIMIT] = compute_q.notional > prior_credit;
        end

        credit_after = prior_credit - compute_q.notional;
        accepted_gross = prior_gross + compute_q.notional;
        decision_d.gross_after = (decision_d.reject == 8'b0) ? accepted_gross : prior_gross;
    end

    always @(posedge clk) begin
        if (rst) begin
            capture_valid <= 1'b0;
            compute_valid <= 1'b0;
            decision_valid <= 1'b0;
            halt_latched <= 1'b0;
        end else begin
            halt_latched <= halted;
            if (capture_ready) begin
                capture_valid <= s_valid && s_ready;
                if (s_valid && s_ready) capture_q <= capture_d;
            end
            if (compute_ready) begin
                compute_valid <= capture_valid;
                if (capture_valid) compute_q <= compute_d;
            end
            if (decision_ready) begin
                decision_valid <= compute_valid;
                if (compute_valid) decision_q <= decision_d;
            end
        end
    end

    integer i;
    always @(posedge clk) begin
        if (rst) begin
            enabled <= '0;
            for (i = 0; i < NUM_INSTRUMENTS; i = i + 1) begin
                buy_gross[i] <= '0;
                sell_gross[i] <= '0;
                buy_credit[i] <= '0;
                sell_credit[i] <= '0;
            end
        end else if (cfg_valid && cfg_ready && cfg_in_range) begin
            enabled[cfg_instrument] <= cfg_enable && cfg_limits_ok;
            min_price[cfg_instrument] <= cfg_min_price;
            max_price[cfg_instrument] <= cfg_max_price;
            max_qty[cfg_instrument] <= cfg_max_qty;
            max_order_notional[cfg_instrument] <= cfg_max_order_notional;
            buy_credit[cfg_instrument] <= cfg_max_buy_gross > buy_gross[cfg_instrument] ?
                cfg_max_buy_gross - buy_gross[cfg_instrument] : '0;
            sell_credit[cfg_instrument] <= cfg_max_sell_gross > sell_gross[cfg_instrument] ?
                cfg_max_sell_gross - sell_gross[cfg_instrument] : '0;
        end else if (reserve) begin
            if (compute_q.order.sell) begin
                sell_gross[compute_q.order.instrument] <= accepted_gross;
                sell_credit[compute_q.order.instrument] <= credit_after;
            end else begin
                buy_gross[compute_q.order.instrument] <= accepted_gross;
                buy_credit[compute_q.order.instrument] <= credit_after;
            end
        end
    end
endmodule

`default_nettype wire
