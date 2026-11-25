`timescale 1ns/1ps
`default_nettype none

// An opt-in account and firm overlay around the base pre-trade risk gateway.
// The native gateway owns order generations, fills and per-key limits.
module risk_portfolio_gateway #(
    parameter integer NUM_ACCOUNTS = 3,
    parameter integer NUM_INSTRUMENTS = 3,
    parameter integer ORDER_SLOTS = 8,
    parameter integer TAG_W = 16,
    parameter integer PRICE_W = 24,
    parameter integer QTY_W = 16,
    parameter integer GEN_W = 8,
    parameter integer EVENT_W = 12,
    localparam integer ACCOUNT_W = NUM_ACCOUNTS > 1 ? $clog2(NUM_ACCOUNTS) : 1,
    localparam integer INSTRUMENT_W = NUM_INSTRUMENTS > 1 ? $clog2(NUM_INSTRUMENTS) : 1,
    localparam integer SLOT_W = ORDER_SLOTS > 1 ? $clog2(ORDER_SLOTS) : 1,
    localparam integer COUNT_W = $clog2(ORDER_SLOTS + 1),
    localparam integer AMOUNT_W = PRICE_W + QTY_W,
    localparam integer TOTAL_W = AMOUNT_W + $clog2(ORDER_SLOTS + 2*NUM_ACCOUNTS*NUM_INSTRUMENTS + 1),
    localparam integer NATIVE_W = 3 + TAG_W + ACCOUNT_W + INSTRUMENT_W + 1 + SLOT_W +
                    GEN_W + EVENT_W + 5 + 1 + QTY_W + PRICE_W + 3*AMOUNT_W + COUNT_W
) (
    input  wire                         clk,
    input  wire                         rst,
    input  wire                         halt_req,
    output wire                         halted,

    input  wire                         cfg_valid,
    output wire                         cfg_ready,
    input  wire [ACCOUNT_W-1:0]         cfg_account,
    input  wire [INSTRUMENT_W-1:0]      cfg_instrument,
    input  wire                         cfg_sell,
    input  wire                         cfg_enable,
    input  wire [PRICE_W-1:0]           cfg_min_price,
    input  wire [PRICE_W-1:0]           cfg_max_price,
    input  wire [QTY_W-1:0]             cfg_max_order_qty,
    input  wire [AMOUNT_W-1:0]          cfg_max_open_notional,
    input  wire [COUNT_W-1:0]           cfg_max_open_orders,

    input  wire                         s_valid,
    output wire                         s_ready,
    input  wire [2:0]                   s_op,
    input  wire [TAG_W-1:0]             s_tag,
    input  wire [ACCOUNT_W-1:0]         s_account,
    input  wire [INSTRUMENT_W-1:0]      s_instrument,
    input  wire                         s_sell,
    input  wire [SLOT_W-1:0]            s_slot,
    input  wire [GEN_W-1:0]             s_generation,
    input  wire [EVENT_W-1:0]           s_event_seq,
    input  wire [PRICE_W-1:0]           s_price,
    input  wire [QTY_W-1:0]             s_qty,

    output wire                         m_valid,
    input  wire                         m_ready,
    output logic [NATIVE_W-1:0]         m_native,
    output logic [2:0]                  m_portfolio_reason,
    output logic [TOTAL_W-1:0]          m_account_gross,
    output logic [TOTAL_W-1:0]          m_account_turnover,
    output logic [COUNT_W-1:0]          m_account_orders,
    output logic [TOTAL_W-1:0]          m_firm_gross,
    output logic [TOTAL_W-1:0]          m_firm_turnover,
    output logic [COUNT_W-1:0]          m_firm_orders,
    output logic [31:0]                 m_epoch,

    input  wire                         ctl_valid,
    output wire                         ctl_ready,
    input  wire [1:0]                   ctl_op,
    input  wire                         ctl_firm,
    input  wire [ACCOUNT_W-1:0]         ctl_account,
    input  wire [2:0]                   ctl_mask,
    input  wire [TOTAL_W-1:0]           ctl_gross_limit,
    input  wire [COUNT_W-1:0]           ctl_count_limit,
    input  wire [TOTAL_W-1:0]           ctl_usage_limit,
    input  wire [31:0]                  ctl_epoch,
    output wire                         ctl_m_valid,
    input  wire                         ctl_m_ready,
    output logic [2:0]                  ctl_m_status,
    output logic [2:0]                  ctl_m_mask,
    output logic [TOTAL_W-1:0]          ctl_m_gross_limit,
    output logic [COUNT_W-1:0]          ctl_m_count_limit,
    output logic [TOTAL_W-1:0]          ctl_m_usage_limit,
    output logic [TOTAL_W-1:0]          ctl_m_gross,
    output logic [TOTAL_W-1:0]          ctl_m_turnover,
    output logic [COUNT_W-1:0]          ctl_m_orders,
    output logic [31:0]                 ctl_m_epoch
);
    localparam [2:0] OP_NEW = 0, OP_FILL = 1, OP_AMEND = 4, OP_SNAPSHOT = 5;
    localparam [4:0] NATIVE_OK = 0, NATIVE_HALTED = 19, PORTFOLIO_LIMIT = 22;
    localparam [1:0] IDLE = 0, CAP_CHECK = 1, FORWARD = 2, WAIT_NATIVE = 3;

    typedef struct packed {
        logic [2:0] op;
        logic [TAG_W-1:0] tag;
        logic [ACCOUNT_W-1:0] account;
        logic [INSTRUMENT_W-1:0] instrument;
        logic sell;
        logic [SLOT_W-1:0] slot;
        logic [GEN_W-1:0] generation;
        logic [EVENT_W-1:0] event_seq;
        logic [PRICE_W-1:0] price;
        logic [QTY_W-1:0] qty;
    } request_t;

    typedef struct packed {
        logic live;
        logic [ACCOUNT_W-1:0] account;
        logic [INSTRUMENT_W-1:0] instrument;
        logic sell;
        logic [GEN_W-1:0] generation;
        logic [EVENT_W-1:0] event_seq;
        logic [AMOUNT_W-1:0] reserved;
    } mirror_t;

    typedef struct packed {
        logic [2:0] op;
        logic [TAG_W-1:0] tag;
        logic [ACCOUNT_W-1:0] account;
        logic [INSTRUMENT_W-1:0] instrument;
        logic sell;
        logic [SLOT_W-1:0] slot;
        logic [GEN_W-1:0] generation;
        logic [EVENT_W-1:0] event_seq;
        logic [4:0] code;
        logic live;
        logic [QTY_W-1:0] leaves;
        logic [PRICE_W-1:0] price;
        logic [AMOUNT_W-1:0] reserved;
        logic [AMOUNT_W-1:0] open_amount;
        logic [AMOUNT_W-1:0] executed_amount;
        logic [COUNT_W-1:0] open_count;
    } receipt_t;

    localparam integer MIRROR_W = 2 + ACCOUNT_W + INSTRUMENT_W + GEN_W + EVENT_W + AMOUNT_W;
    logic [MIRROR_W-1:0] slot_image [0:ORDER_SLOTS-1];
    logic [1:0] state;
    logic reply_valid, control_reply_valid;
    logic [2:0] held_denial;
    request_t request_q;
    mirror_t prior;
    receipt_t native, published;
    wire native_in_ready, native_out_valid, native_cfg_ready;
    wire room = !rst && state == IDLE && !reply_valid && !control_reply_valid;
    wire cfg_to_native = room && cfg_valid;
    wire control_fire = ctl_valid && ctl_ready;
    wire request_fire = s_valid && s_ready;
    wire native_transfer = state == WAIT_NATIVE && native_out_valid;

    assign cfg_ready = room && native_cfg_ready;
    assign ctl_ready = room && !cfg_valid;
    assign s_ready = room && !cfg_valid && !ctl_valid;
    assign m_valid = !rst && reply_valid;
    assign ctl_m_valid = !rst && control_reply_valid;

    wire account_ok = {1'b0, request_q.account} < (ACCOUNT_W+1)'(NUM_ACCOUNTS);
    wire instrument_ok = {1'b0, request_q.instrument} < (INSTRUMENT_W+1)'(NUM_INSTRUMENTS);
    wire slot_ok = {1'b0, request_q.slot} < (SLOT_W+1)'(ORDER_SLOTS);
    assign prior = slot_ok ? slot_image[request_q.slot] : '0;
    wire own_prior = prior.live && {prior.account, prior.instrument, prior.sell} ==
                                  {request_q.account, request_q.instrument, request_q.sell};
    wire new_shape = request_q.op == OP_NEW && !prior.live && !(&prior.generation) &&
                     request_q.generation == '0 && request_q.event_seq == '0;
    wire amend_shape = request_q.op == OP_AMEND && own_prior &&
                       request_q.generation == prior.generation &&
                       !(&prior.event_seq) && !(&request_q.event_seq) &&
                       request_q.event_seq == prior.event_seq + 1'b1;
    wire [AMOUNT_W-1:0] requested_amount = request_q.price * request_q.qty;
    wire [AMOUNT_W-1:0] old_reservation = prior.live ? prior.reserved : '0;
    wire expansion = account_ok && instrument_ok && slot_ok && !halted &&
                     (new_shape || amend_shape) && requested_amount > old_reservation;

    wire native_changes = native.code == NATIVE_OK && held_denial == '0 &&
                          request_q.op <= OP_AMEND;
    wire apply = native_transfer && native_changes;
    wire [AMOUNT_W-1:0] execution = request_q.op == OP_FILL ? requested_amount : '0;
    always @* begin
        published = native;
        if (held_denial != '0) begin
            published.op = request_q.op;
            published.code = halted ? NATIVE_HALTED : PORTFOLIO_LIMIT;
        end
    end

    wire [2:0] live_denial, control_status, view_mask;
    wire [TOTAL_W-1:0] account_gross, account_turnover, firm_gross, firm_turnover;
    wire [COUNT_W-1:0] account_orders, firm_orders;
    wire [TOTAL_W-1:0] view_gross_limit, view_usage_limit, view_gross, view_turnover;
    wire [COUNT_W-1:0] view_count_limit, view_orders;
    wire [31:0] view_epoch, epoch;

    risk_portfolio_ledger #(
        .ACCOUNTS(NUM_ACCOUNTS), .SLOTS(ORDER_SLOTS), .AMOUNT_W(AMOUNT_W), .TOTAL_W(TOTAL_W)
    ) totals (
        .clk, .rst, .account(request_q.account), .should_check(expansion),
        .prior_reserve(old_reservation), .proposed_reserve(requested_amount),
        .proposed_new(new_shape), .denial(live_denial),
        .apply_delta(apply), .committed_reserve(native.reserved), .prior_live(prior.live),
        .committed_live(native.live), .committed_execution(execution),
        .account_gross, .account_turnover, .account_orders, .firm_gross, .firm_turnover, .firm_orders,
        .ctl_fire(control_fire), .ctl_op, .ctl_firm, .ctl_account, .ctl_mask, .ctl_gross_limit,
        .ctl_count_limit, .ctl_usage_limit, .ctl_epoch, .ctl_status(control_status),
        .view_mask, .view_gross_limit, .view_count_limit, .view_usage_limit,
        .view_gross, .view_turnover, .view_orders, .view_epoch, .epoch
    );

    // A portfolio-limit decision issues a read-only native snapshot so all
    // reply fields retain the wire semantics of a native refusal.
    hft_order_risk_gateway #(
        .NUM_ACCOUNTS(NUM_ACCOUNTS), .NUM_INSTRUMENTS(NUM_INSTRUMENTS), .ORDER_SLOTS(ORDER_SLOTS),
        .TAG_W(TAG_W), .PRICE_W(PRICE_W), .QTY_W(QTY_W), .GEN_W(GEN_W), .EVENT_W(EVENT_W)
    ) core (
        .clk, .rst, .halt_req, .halted, .cfg_valid(cfg_to_native), .cfg_ready(native_cfg_ready),
        .cfg_account, .cfg_instrument, .cfg_sell, .cfg_enable, .cfg_min_price, .cfg_max_price,
        .cfg_max_order_qty, .cfg_max_open_notional, .cfg_max_open_orders,
        .s_valid(state == FORWARD), .s_ready(native_in_ready),
        .s_op(held_denial != '0 ? OP_SNAPSHOT : request_q.op), .s_tag(request_q.tag),
        .s_account(request_q.account), .s_instrument(request_q.instrument), .s_sell(request_q.sell),
        .s_slot(request_q.slot), .s_generation(request_q.generation), .s_event_seq(request_q.event_seq),
        .s_price(request_q.price), .s_qty(request_q.qty),
        .m_valid(native_out_valid), .m_ready(state == WAIT_NATIVE),
        .m_op(native.op), .m_tag(native.tag), .m_account(native.account),
        .m_instrument(native.instrument), .m_sell(native.sell), .m_slot(native.slot),
        .m_generation(native.generation), .m_event_seq(native.event_seq), .m_code(native.code),
        .m_slot_live(native.live), .m_leaves(native.leaves), .m_limit_price(native.price),
        .m_reserved(native.reserved), .m_open_notional(native.open_amount),
        .m_executed_notional(native.executed_amount), .m_open_orders(native.open_count)
    );

    always @(posedge clk) begin
        if (rst) begin
            state <= IDLE;
            request_q <= '0;
            held_denial <= '0;
            reply_valid <= 1'b0;
            control_reply_valid <= 1'b0;
            m_native <= '0;
            m_portfolio_reason <= '0;
            m_account_gross <= '0;
            m_account_turnover <= '0;
            m_account_orders <= '0;
            m_firm_gross <= '0;
            m_firm_turnover <= '0;
            m_firm_orders <= '0;
            m_epoch <= '0;
            ctl_m_status <= '0;
            ctl_m_mask <= '0;
            ctl_m_gross_limit <= '0;
            ctl_m_count_limit <= '0;
            ctl_m_usage_limit <= '0;
            ctl_m_gross <= '0;
            ctl_m_turnover <= '0;
            ctl_m_orders <= '0;
            ctl_m_epoch <= '0;
            for (integer idx = 0; idx < ORDER_SLOTS; idx = idx + 1)
                slot_image[idx] <= '0;
        end else begin
            if (m_valid && m_ready)
                reply_valid <= 1'b0;
            if (ctl_m_valid && ctl_m_ready)
                control_reply_valid <= 1'b0;
            if (control_fire) begin
                control_reply_valid <= 1'b1;
                ctl_m_status <= control_status;
                ctl_m_mask <= view_mask;
                ctl_m_gross_limit <= view_gross_limit;
                ctl_m_count_limit <= view_count_limit;
                ctl_m_usage_limit <= view_usage_limit;
                ctl_m_gross <= view_gross;
                ctl_m_turnover <= view_turnover;
                ctl_m_orders <= view_orders;
                ctl_m_epoch <= view_epoch;
            end
            case (state)
                IDLE: if (request_fire) begin
                    request_q <= {s_op, s_tag, s_account, s_instrument, s_sell, s_slot,
                                  s_generation, s_event_seq, s_price, s_qty};
                    state <= CAP_CHECK;
                end
                CAP_CHECK: begin
                    held_denial <= live_denial;
                    state <= FORWARD;
                end
                FORWARD: if (native_in_ready)
                    state <= WAIT_NATIVE;
                WAIT_NATIVE: if (native_transfer) begin
                    if (apply)
                        slot_image[request_q.slot] <= {native.live, native.account, native.instrument,
                                                       native.sell, native.generation, native.event_seq,
                                                       native.reserved};
                    m_native <= published;
                    m_portfolio_reason <= halted ? '0 : held_denial;
                    m_account_gross <= account_gross;
                    m_account_turnover <= account_turnover;
                    m_account_orders <= account_orders;
                    m_firm_gross <= firm_gross;
                    m_firm_turnover <= firm_turnover;
                    m_firm_orders <= firm_orders;
                    m_epoch <= epoch;
                    reply_valid <= 1'b1;
                    state <= IDLE;
                end
                default: state <= IDLE;
            endcase
        end
    end

`ifdef FORMAL
    always @(posedge clk) begin
        if (!rst && $past(!rst)) begin
            if ($past(m_valid && !m_ready)) begin
                assert(m_valid);
                assert($stable({m_native, m_portfolio_reason, m_account_gross, m_account_turnover,
                                m_account_orders, m_firm_gross, m_firm_turnover, m_firm_orders, m_epoch}));
            end
            if ($past(ctl_m_valid && !ctl_m_ready)) begin
                assert(ctl_m_valid);
                assert($stable({ctl_m_status, ctl_m_mask, ctl_m_gross_limit, ctl_m_count_limit,
                                ctl_m_usage_limit, ctl_m_gross, ctl_m_turnover, ctl_m_orders, ctl_m_epoch}));
            end
            assert(!apply || (slot_ok && account_ok));
            assert(!apply || held_denial == 0);
            assert(firm_orders <= COUNT_W'(ORDER_SLOTS));
            assert(account_orders <= firm_orders);
            assert(account_gross <= firm_gross);
            assert(account_turnover <= firm_turnover);
            assert(epoch >= $past(epoch));
        end
    end
`endif
endmodule

`default_nettype wire
