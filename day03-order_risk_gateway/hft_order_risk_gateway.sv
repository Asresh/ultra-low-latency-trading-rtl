`timescale 1ns/1ps
`default_nettype none

module hft_order_risk_gateway #(
    parameter integer NUM_ACCOUNTS = 2,
    parameter integer NUM_INSTRUMENTS = 2,
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
    localparam integer NOTIONAL_W = PRICE_W + QTY_W
) (
    input  wire                   clk,
    input  wire                   rst,
    input  wire                   halt_req,
    output wire                   halted,
    input  wire                   cfg_valid,
    output wire                   cfg_ready,
    input  wire [ACCOUNT_W-1:0]   cfg_account,
    input  wire [INSTRUMENT_W-1:0] cfg_instrument,
    input  wire                   cfg_sell,
    input  wire                   cfg_enable,
    input  wire [PRICE_W-1:0]     cfg_min_price,
    input  wire [PRICE_W-1:0]     cfg_max_price,
    input  wire [QTY_W-1:0]       cfg_max_order_qty,
    input  wire [NOTIONAL_W-1:0]  cfg_max_open_notional,
    input  wire [COUNT_W-1:0]     cfg_max_open_orders,
    input  wire                   s_valid,
    output wire                   s_ready,
    input  wire [2:0]             s_op,
    input  wire [TAG_W-1:0]       s_tag,
    input  wire [ACCOUNT_W-1:0]   s_account,
    input  wire [INSTRUMENT_W-1:0] s_instrument,
    input  wire                   s_sell,
    input  wire [SLOT_W-1:0]      s_slot,
    input  wire [GEN_W-1:0]       s_generation,
    input  wire [EVENT_W-1:0]     s_event_seq,
    input  wire [PRICE_W-1:0]     s_price,
    input  wire [QTY_W-1:0]       s_qty,
    output wire                   m_valid,
    input  wire                   m_ready,
    output wire [2:0]             m_op,
    output wire [TAG_W-1:0]       m_tag,
    output wire [ACCOUNT_W-1:0]   m_account,
    output wire [INSTRUMENT_W-1:0] m_instrument,
    output wire                   m_sell,
    output wire [SLOT_W-1:0]      m_slot,
    output wire [GEN_W-1:0]       m_generation,
    output wire [EVENT_W-1:0]     m_event_seq,
    output wire [4:0]             m_code,
    output wire                   m_slot_live,
    output wire [QTY_W-1:0]       m_leaves,
    output wire [PRICE_W-1:0]     m_limit_price,
    output wire [NOTIONAL_W-1:0]  m_reserved,
    output wire [NOTIONAL_W-1:0]  m_open_notional,
    output wire [NOTIONAL_W-1:0]  m_executed_notional,
    output wire [COUNT_W-1:0]     m_open_orders
);
    localparam [2:0] OP_NEW = 0, OP_FILL = 1, OP_CANCEL = 2,
                     OP_REJECT = 3, OP_AMEND = 4, OP_SNAPSHOT = 5;
    localparam [4:0] OK = 0, BAD_OP = 1, BAD_KEY = 2, BAD_SLOT = 3,
                     BAD_FIELDS = 4, SLOT_BUSY = 5, GENERATION_EXHAUSTED = 6,
                     NOT_LIVE = 7, WRONG_OWNER = 8, STALE_GENERATION = 9,
                     EVENT_SEQUENCE = 10, DISABLED = 11, QTY_LIMIT = 12,
                     PRICE_LIMIT = 13, OPEN_NOTIONAL = 14, OPEN_COUNT = 15,
                     OVERFILL = 16, REJECT_AFTER_FILL = 17, EXEC_OVERFLOW = 18,
                     HALTED = 19, LEDGER_INVARIANT = 20, EVENT_EXHAUSTED = 21;
    localparam [1:0] IDLE = 0, LOOKUP = 1, ARITHMETIC = 2, COMMIT = 3;
    localparam integer KEY_W = ACCOUNT_W + INSTRUMENT_W + 1;
    localparam integer KEY_COUNT = 1 << KEY_W;
    localparam [ACCOUNT_W:0] ACCOUNT_COUNT = (ACCOUNT_W+1)'(NUM_ACCOUNTS);
    localparam [INSTRUMENT_W:0] INSTRUMENT_COUNT = (INSTRUMENT_W+1)'(NUM_INSTRUMENTS);
    localparam [SLOT_W:0] SLOT_COUNT = (SLOT_W+1)'(ORDER_SLOTS);
    localparam [COUNT_W-1:0] MAX_COUNT = COUNT_W'(ORDER_SLOTS);
    localparam [GEN_W-1:0] LAST_GENERATION = {GEN_W{1'b1}};
    localparam [EVENT_W-1:0] LAST_EVENT = {EVENT_W{1'b1}};

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
        logic enabled;
        logic [PRICE_W-1:0] min_price;
        logic [PRICE_W-1:0] max_price;
        logic [QTY_W-1:0] max_qty;
        logic [NOTIONAL_W-1:0] max_open;
        logic [COUNT_W-1:0] max_count;
    } policy_t;

    typedef struct packed {
        logic [NOTIONAL_W-1:0] open_amount;
        logic [NOTIONAL_W-1:0] executed_amount;
        logic [COUNT_W-1:0] open_count;
    } ledger_t;

    typedef struct packed {
        logic live;
        logic had_fill;
        logic [KEY_W-1:0] owner;
        logic [GEN_W-1:0] generation;
        logic [EVENT_W-1:0] event_seq;
        logic [PRICE_W-1:0] limit_price;
        logic [QTY_W-1:0] leaves;
        logic [NOTIONAL_W-1:0] reserved;
    } slot_t;

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
        logic [PRICE_W-1:0] limit_price;
        logic [NOTIONAL_W-1:0] reserved;
        logic [NOTIONAL_W-1:0] open_amount;
        logic [NOTIONAL_W-1:0] executed_amount;
        logic [COUNT_W-1:0] open_count;
    } reply_t;

    // Explicit vector memories keep word layout portable across SV frontends.
    localparam integer POLICY_BITS = 1 + 2 * PRICE_W + QTY_W + NOTIONAL_W + COUNT_W;
    localparam integer LEDGER_BITS = 2 * NOTIONAL_W + COUNT_W;
    localparam integer SLOT_BITS = 2 + KEY_W + GEN_W + EVENT_W + PRICE_W + QTY_W + NOTIONAL_W;
    localparam integer REPLY_BITS = 3 + TAG_W + KEY_W + SLOT_W + GEN_W + EVENT_W + 5 + 1 +
                                    QTY_W + PRICE_W + 3 * NOTIONAL_W + COUNT_W;
    logic [POLICY_BITS-1:0] policy_by_key [0:KEY_COUNT-1];
    logic [LEDGER_BITS-1:0] ledger_by_key [0:KEY_COUNT-1];
    logic [SLOT_BITS-1:0] order_by_slot [0:ORDER_SLOTS-1];
    logic [REPLY_BITS-1:0] replies [0:1];

    request_t req;
    policy_t policy_q, config_value;
    ledger_t ledger_q, ledger_next;
    slot_t slot_q, slot_next;
    reply_t reply_d, reply_view;
    logic [1:0] state;
    logic halt_latched;
    logic fault;
    logic [NOTIONAL_W-1:0] request_amount, fill_release, open_without_slot;
    logic update;
    logic reply_read, reply_write;
    logic [1:0] reply_count;

    wire [KEY_W-1:0] req_key = {req.account, req.instrument, req.sell};
    wire [KEY_W-1:0] cfg_key = {cfg_account, cfg_instrument, cfg_sell};
    wire key_ok = {1'b0, req.account} < ACCOUNT_COUNT &&
                  {1'b0, req.instrument} < INSTRUMENT_COUNT;
    wire cfg_key_ok = {1'b0, cfg_account} < ACCOUNT_COUNT &&
                      {1'b0, cfg_instrument} < INSTRUMENT_COUNT;
    wire slot_ok = {1'b0, req.slot} < SLOT_COUNT;
    wire own_slot = slot_q.generation != '0 && slot_q.owner == req_key;
    wire slot_accounted = ledger_q.open_count != '0 && slot_q.reserved <= ledger_q.open_amount;
    wire push = state == COMMIT;
    wire pop = m_valid && m_ready;

    assign cfg_ready = !rst && state == IDLE && reply_count == 0;
    assign s_ready = !rst && state == IDLE && !cfg_valid && (reply_count < 2 || pop);
    assign m_valid = !rst && reply_count != 0;
    assign halted = halt_latched || halt_req;
    assign reply_view = replies[reply_read];

    assign m_op = reply_view.op;
    assign m_tag = reply_view.tag;
    assign m_account = reply_view.account;
    assign m_instrument = reply_view.instrument;
    assign m_sell = reply_view.sell;
    assign m_slot = reply_view.slot;
    assign m_generation = reply_view.generation;
    assign m_event_seq = reply_view.event_seq;
    assign m_code = reply_view.code;
    assign m_slot_live = reply_view.live;
    assign m_leaves = reply_view.leaves;
    assign m_limit_price = reply_view.limit_price;
    assign m_reserved = reply_view.reserved;
    assign m_open_notional = reply_view.open_amount;
    assign m_executed_notional = reply_view.executed_amount;
    assign m_open_orders = reply_view.open_count;

    always @* begin
        config_value.enabled = cfg_enable && cfg_min_price != '0 &&
                               cfg_min_price <= cfg_max_price && cfg_max_order_qty != '0 &&
                               cfg_max_open_notional != '0 && cfg_max_open_orders != '0 &&
                               cfg_max_open_orders <= MAX_COUNT;
        config_value.min_price = cfg_min_price;
        config_value.max_price = cfg_max_price;
        config_value.max_qty = cfg_max_order_qty;
        config_value.max_open = cfg_max_open_notional;
        config_value.max_count = cfg_max_open_orders;
    end

    function automatic [4:0] admission_status(
        input policy_t policy,
        input logic [PRICE_W-1:0] price,
        input logic [QTY_W-1:0] qty,
        input logic [NOTIONAL_W:0] projected_open,
        input logic [COUNT_W:0] projected_count,
        input logic stop
    );
        if (stop)
            admission_status = HALTED;
        else if (!policy.enabled)
            admission_status = DISABLED;
        else if (qty > policy.max_qty)
            admission_status = QTY_LIMIT;
        else if (price < policy.min_price || price > policy.max_price)
            admission_status = PRICE_LIMIT;
        else if (projected_open > {1'b0, policy.max_open})
            admission_status = OPEN_NOTIONAL;
        else if (projected_count > {1'b0, policy.max_count})
            admission_status = OPEN_COUNT;
        else
            admission_status = OK;
    endfunction

    function automatic slot_t terminate_slot(input slot_t prior, input logic [EVENT_W-1:0] sequence_id);
        slot_t cleared;
        begin
            cleared = prior;
            cleared.live = 1'b0;
            cleared.event_seq = sequence_id;
            cleared.leaves = '0;
            cleared.reserved = '0;
            terminate_slot = cleared;
        end
    endfunction

    logic [4:0] code;
    logic visible_slot;
    logic [NOTIONAL_W:0] requested_open, executed_after;
    logic [COUNT_W:0] requested_count;

    // Propose a complete write set, validate it, then atomically commit in the controller.
    always @* begin
        slot_next = slot_q;
        ledger_next = ledger_q;
        update = 1'b0;
        fault = 1'b0;
        code = OK;
        requested_open = {1'b0, ledger_q.open_amount} + {1'b0, request_amount};
        requested_count = {1'b0, ledger_q.open_count} + 1'b1;
        executed_after = {1'b0, ledger_q.executed_amount} + {1'b0, request_amount};

        if (req.op > OP_SNAPSHOT) begin
            code = BAD_OP;
        end else if (!key_ok) begin
            code = BAD_KEY;
        end else if (!slot_ok) begin
            code = BAD_SLOT;
        end else if (req.op == OP_NEW) begin
            if (req.generation != '0 || req.event_seq != '0 || req.price == '0 || req.qty == '0) begin
                code = BAD_FIELDS;
            end else if (slot_q.live) begin
                code = SLOT_BUSY;
            end else if (slot_q.generation == LAST_GENERATION) begin
                code = GENERATION_EXHAUSTED;
            end else begin
                code = admission_status(policy_q, req.price, req.qty, requested_open, requested_count, halted);
                if (code == OK) begin
                    slot_next.live = 1'b1;
                    slot_next.had_fill = 1'b0;
                    slot_next.owner = req_key;
                    slot_next.generation = slot_q.generation + 1'b1;
                    slot_next.event_seq = '0;
                    slot_next.limit_price = req.price;
                    slot_next.leaves = req.qty;
                    slot_next.reserved = request_amount;
                    ledger_next.open_amount = requested_open[NOTIONAL_W-1:0];
                    ledger_next.open_count = requested_count[COUNT_W-1:0];
                    update = 1'b1;
                end
            end
        end else if (req.op != OP_SNAPSHOT) begin
            if (((req.op == OP_FILL || req.op == OP_AMEND) && (req.price == '0 || req.qty == '0)) ||
                ((req.op == OP_CANCEL || req.op == OP_REJECT) && (req.price != '0 || req.qty != '0))) begin
                code = BAD_FIELDS;
            end else if (!slot_q.live) begin
                code = NOT_LIVE;
            end else if (!own_slot) begin
                code = WRONG_OWNER;
            end else if (req.generation != slot_q.generation) begin
                code = STALE_GENERATION;
            end else if (slot_q.event_seq == LAST_EVENT) begin
                code = EVENT_EXHAUSTED;
            end else if (req.event_seq != slot_q.event_seq + 1'b1) begin
                code = EVENT_SEQUENCE;
            end else if (!slot_accounted) begin
                code = LEDGER_INVARIANT;
                fault = 1'b1;
            end else begin
                case (req.op)
                    OP_FILL: begin
                        if (req.qty > slot_q.leaves) begin
                            code = OVERFILL;
                        end else if (executed_after[NOTIONAL_W]) begin
                            code = EXEC_OVERFLOW;
                            fault = 1'b1;
                        end else if (fill_release > slot_q.reserved) begin
                            code = LEDGER_INVARIANT;
                            fault = 1'b1;
                        end else if (req.event_seq == LAST_EVENT && req.qty != slot_q.leaves) begin
                            code = EVENT_EXHAUSTED;
                        end else begin
                            ledger_next.executed_amount = executed_after[NOTIONAL_W-1:0];
                            ledger_next.open_amount = ledger_q.open_amount - fill_release;
                            if (req.qty == slot_q.leaves) begin
                                slot_next = terminate_slot(slot_q, req.event_seq);
                                ledger_next.open_count = ledger_q.open_count - 1'b1;
                            end else begin
                                slot_next.leaves = slot_q.leaves - req.qty;
                                slot_next.reserved = slot_q.reserved - fill_release;
                                slot_next.event_seq = req.event_seq;
                            end
                            slot_next.had_fill = 1'b1;
                            update = 1'b1;
                        end
                    end
                    OP_CANCEL, OP_REJECT: begin
                        if (req.op == OP_REJECT && slot_q.had_fill) begin
                            code = REJECT_AFTER_FILL;
                        end else begin
                            ledger_next.open_amount = open_without_slot;
                            ledger_next.open_count = ledger_q.open_count - 1'b1;
                            slot_next = terminate_slot(slot_q, req.event_seq);
                            update = 1'b1;
                        end
                    end
                    OP_AMEND: begin
                        requested_open = {1'b0, open_without_slot} +
                                         {1'b0, request_amount};
                        if (req.event_seq == LAST_EVENT) begin
                            code = EVENT_EXHAUSTED;
                        end else begin
                            code = admission_status(policy_q, req.price, req.qty, requested_open,
                                                    {1'b0, ledger_q.open_count}, halted);
                            if (code == OK) begin
                                ledger_next.open_amount = requested_open[NOTIONAL_W-1:0];
                                slot_next.leaves = req.qty;
                                slot_next.limit_price = req.price;
                                slot_next.reserved = request_amount;
                                slot_next.event_seq = req.event_seq;
                                update = 1'b1;
                            end
                        end
                    end
                    default: code = BAD_OP;
                endcase
            end
        end

        visible_slot = key_ok && slot_ok &&
                       (own_slot || (update && req.op == OP_NEW));
        reply_d = '0;
        reply_d.op = req.op;
        reply_d.tag = req.tag;
        reply_d.account = req.account;
        reply_d.instrument = req.instrument;
        reply_d.sell = req.sell;
        reply_d.slot = req.slot;
        reply_d.code = code;
        if (visible_slot) begin
            reply_d.generation = slot_next.generation;
            reply_d.event_seq = slot_next.event_seq;
            reply_d.live = slot_next.live;
            reply_d.leaves = slot_next.leaves;
            reply_d.limit_price = slot_next.limit_price;
            reply_d.reserved = slot_next.reserved;
        end
        if (key_ok) begin
            reply_d.open_amount = ledger_next.open_amount;
            reply_d.executed_amount = ledger_next.executed_amount;
            reply_d.open_count = ledger_next.open_count;
        end
    end

    integer idx;
    always @(posedge clk) begin
        if (rst) begin
            state <= IDLE;
            halt_latched <= 1'b0;
            for (idx = 0; idx < KEY_COUNT; idx = idx + 1) begin
                policy_by_key[idx] <= '0;
                ledger_by_key[idx] <= '0;
            end
            for (idx = 0; idx < ORDER_SLOTS; idx = idx + 1)
                order_by_slot[idx] <= '0;
        end else begin
            halt_latched <= halted || (push && fault);
            case (state)
                IDLE: begin
                    if (cfg_valid && cfg_ready && cfg_key_ok)
                        policy_by_key[cfg_key] <= config_value;
                    if (s_valid && s_ready) begin
                        req <= {s_op, s_tag, s_account, s_instrument, s_sell, s_slot,
                                s_generation, s_event_seq, s_price, s_qty};
                        state <= LOOKUP;
                    end
                end
                LOOKUP: begin
                    policy_q <= key_ok ? policy_by_key[req_key] : '0;
                    ledger_q <= key_ok ? ledger_by_key[req_key] : '0;
                    slot_q <= slot_ok ? order_by_slot[req.slot] : '0;
                    state <= ARITHMETIC;
                end
                ARITHMETIC: begin
                    request_amount <= req.price * req.qty;
                    fill_release <= slot_q.limit_price * req.qty;
                    open_without_slot <= ledger_q.open_amount - slot_q.reserved;
                    state <= COMMIT;
                end
                COMMIT: begin
                    if (update) begin
                        order_by_slot[req.slot] <= slot_next;
                        ledger_by_key[req_key] <= ledger_next;
                    end
                    state <= IDLE;
                end
                default: state <= IDLE;
            endcase
        end
    end

    always @(posedge clk) begin
        if (rst) begin
            reply_count <= 0;
            reply_read <= 0;
            reply_write <= 0;
        end else begin
            if (pop)
                reply_read <= ~reply_read;
            if (push) begin
                replies[reply_write] <= reply_d;
                reply_write <= ~reply_write;
            end
            case ({push, pop})
                2'b01: reply_count <= reply_count - 1'b1;
                2'b10: reply_count <= reply_count + 1'b1;
                default: reply_count <= reply_count;
            endcase
        end
    end
endmodule

`default_nettype wire
