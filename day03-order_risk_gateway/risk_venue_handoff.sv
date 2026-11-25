`timescale 1ns/1ps
`default_nettype none

// The risk ledger reserves before a new order is offered to the venue adapter.
// An accepted egress qualifier is the boundary at which absence of a venue
// order can no longer be assumed. Only the independent report lane may then
// release that reservation.
module risk_venue_handoff #(
    parameter integer NUM_ACCOUNTS = 3,
    parameter integer NUM_INSTRUMENTS = 3,
    parameter integer ORDER_SLOTS = 8,
    parameter integer VENUES = 4,
    parameter integer TAG_W = 16,
    parameter integer PRICE_W = 24,
    parameter integer QTY_W = 16,
    parameter integer GEN_W = 8,
    parameter integer EVENT_W = 12,
    parameter integer LEASE_W = 16,
    parameter integer TIMER_W = 16,
    parameter integer REPORT_W = 16,
    parameter integer MATCH_W = 48,
    parameter integer HISTORY = 8,
    parameter integer ACTION_W = 8,
    localparam integer ACCOUNT_W = NUM_ACCOUNTS > 1 ? $clog2(NUM_ACCOUNTS) : 1,
    localparam integer INSTRUMENT_W = NUM_INSTRUMENTS > 1 ? $clog2(NUM_INSTRUMENTS) : 1,
    localparam integer SLOT_W = ORDER_SLOTS > 1 ? $clog2(ORDER_SLOTS) : 1,
    localparam integer VENUE_W = $clog2(VENUES+1),
    localparam integer COUNT_W = $clog2(ORDER_SLOTS+1),
    localparam integer AMOUNT_W = PRICE_W + QTY_W,
    localparam integer TOTAL_W = AMOUNT_W + $clog2(ORDER_SLOTS + 2*NUM_ACCOUNTS*NUM_INSTRUMENTS + 1),
    localparam integer NATIVE_W = 3 + TAG_W + ACCOUNT_W + INSTRUMENT_W + 1 + SLOT_W +
        GEN_W + EVENT_W + 5 + 1 + QTY_W + PRICE_W + 3*AMOUNT_W + COUNT_W
) (
    input wire clk, rst,
    input wire halt_req,
    output wire halted,
    output logic handoff_fault,

    input wire cfg_valid,
    output wire cfg_ready,
    input wire [ACCOUNT_W-1:0] cfg_account,
    input wire [INSTRUMENT_W-1:0] cfg_instrument,
    input wire cfg_sell, cfg_enable,
    input wire [PRICE_W-1:0] cfg_min_price, cfg_max_price,
    input wire [QTY_W-1:0] cfg_max_order_qty,
    input wire [AMOUNT_W-1:0] cfg_max_open_notional,
    input wire [COUNT_W-1:0] cfg_max_open_orders,

    input wire ctl_valid,
    output wire ctl_ready,
    input wire [1:0] ctl_op,
    input wire ctl_firm,
    input wire [ACCOUNT_W-1:0] ctl_account,
    input wire [2:0] ctl_mask,
    input wire [TOTAL_W-1:0] ctl_gross_limit,
    input wire [COUNT_W-1:0] ctl_count_limit,
    input wire [TOTAL_W-1:0] ctl_usage_limit,
    input wire [31:0] ctl_epoch,
    output wire ctl_m_valid,
    input wire ctl_m_ready,
    output wire [2:0] ctl_m_status, ctl_m_mask,
    output wire [TOTAL_W-1:0] ctl_m_gross_limit,
    output wire [COUNT_W-1:0] ctl_m_count_limit,
    output wire [TOTAL_W-1:0] ctl_m_usage_limit, ctl_m_gross, ctl_m_turnover,
    output wire [COUNT_W-1:0] ctl_m_orders,
    output wire [31:0] ctl_m_epoch,

    input wire time_tick,
    input wire [VENUES-1:0] link_loss,
    input wire heartbeat_valid,
    output wire heartbeat_ready,
    input wire [VENUE_W-1:0] heartbeat_venue,
    input wire [LEASE_W-1:0] heartbeat_epoch,
    input wire g_valid,
    output wire g_ready,
    input wire [1:0] g_op,
    input wire [VENUE_W-1:0] g_venue,
    input wire [LEASE_W-1:0] g_epoch,
    input wire [TIMER_W-1:0] g_timeout,
    output wire g_m_valid,
    input wire g_m_ready,
    output wire [2:0] g_m_status,
    output wire [1:0] g_m_op,
    output wire [VENUE_W-1:0] g_m_venue,
    output wire [LEASE_W-1:0] g_m_epoch,
    output wire [TIMER_W-1:0] g_m_countdown,
    output wire g_m_active,
    output wire [VENUES-1:0] venue_allowed,
    output wire [VENUES*LEASE_W-1:0] venue_epochs,
    output wire [31:0] ignored_heartbeats, lease_expirations,

    input wire s_valid,
    output wire s_ready,
    input wire s_cancel,
    input wire [TAG_W-1:0] s_tag,
    input wire [VENUE_W-1:0] s_venue,
    input wire [LEASE_W-1:0] s_order_epoch,
    input wire [ACCOUNT_W-1:0] s_account,
    input wire [INSTRUMENT_W-1:0] s_instrument,
    input wire s_sell,
    input wire [SLOT_W-1:0] s_slot,
    input wire [GEN_W-1:0] s_generation,
    input wire [PRICE_W-1:0] s_price,
    input wire [QTY_W-1:0] s_qty,

    input wire r_valid,
    output wire r_ready,
    input wire [2:0] r_kind,
    input wire [TAG_W-1:0] r_tag,
    input wire [VENUE_W-1:0] r_venue,
    input wire [LEASE_W-1:0] r_order_epoch,
    input wire [SLOT_W-1:0] r_slot,
    input wire [GEN_W-1:0] r_generation,
    input wire [REPORT_W-1:0] r_sequence,
    input wire [PRICE_W-1:0] r_price,
    input wire [QTY_W-1:0] r_qty,
    input wire [MATCH_W-1:0] r_match,
    input wire [ACTION_W-1:0] r_action,

    output wire tx_valid,
    input wire tx_ready,
    output wire tx_applicable,
    output logic tx_cancel,
    output logic [TAG_W-1:0] tx_order_tag,
    output logic [VENUE_W-1:0] tx_venue,
    output logic [LEASE_W-1:0] tx_order_epoch, tx_session_epoch,
    output logic [ACCOUNT_W-1:0] tx_account,
    output logic [INSTRUMENT_W-1:0] tx_instrument,
    output logic tx_sell,
    output logic [SLOT_W-1:0] tx_slot,
    output logic [GEN_W-1:0] tx_generation,
    output logic [ACTION_W-1:0] tx_action,
    output logic [PRICE_W-1:0] tx_price,
    output logic [QTY_W-1:0] tx_qty,

    output wire m_valid,
    input wire m_ready,
    output logic [1:0] m_source,
    output logic [4:0] m_status,
    output logic [TAG_W-1:0] m_tag,
    output logic [VENUE_W-1:0] m_venue,
    output logic [LEASE_W-1:0] m_order_epoch,
    output logic [SLOT_W-1:0] m_slot,
    output logic [GEN_W-1:0] m_generation,
    output logic [REPORT_W-1:0] m_sequence,
    output logic m_native_valid,
    output logic [NATIVE_W-1:0] m_native,
    output logic [2:0] m_portfolio_reason,
    output logic [TOTAL_W-1:0] m_account_gross, m_account_turnover,
    output logic [COUNT_W-1:0] m_account_orders,
    output logic [TOTAL_W-1:0] m_firm_gross, m_firm_turnover,
    output logic [COUNT_W-1:0] m_firm_orders,
    output logic [31:0] m_risk_epoch
);
    localparam logic [2:0] N_NEW = 0, N_FILL = 1, N_CANCEL = 2, N_REJECT = 3;
    localparam logic [2:0] R_ACK = 0, R_FILL = 1, R_CANCELED = 2,
                           R_REJECT = 3, R_CANCEL_REJECT = 4, R_EXPIRED = 5;
    localparam logic [4:0] APPLIED = 0, RISK_REFUSED = 1, LEASE_CLOSED = 2,
                           BAD_ADDRESS = 3, BAD_FIELDS = 4, WRONG_ORDER = 5,
                           UNSENT = 6, REPLAY = 7, OLD_REPORT = 8, REPORT_GAP = 9,
                           CHANGED_REPORT = 10, REUSED_MATCH = 11, WRONG_PHASE = 12,
                           CANCEL_QUEUED = 13, ENTRY_ACKED = 14, CANCEL_REFUSED = 15,
                           ROLLED_BACK = 16, LAST_SEQUENCE = 17,
                           WRONG_CANCEL_ACTION = 18, ACTION_EXHAUSTED = 19;
    localparam logic [1:0] STRATEGY = 0, REPORT = 1, RECOVERY = 2;
    localparam logic [1:0] IDLE = 0, ISSUE = 1, AWAIT = 2;
    localparam logic [1:0] NONE = 0, QUEUED = 1, AWAIT_CANCEL = 2;
    localparam integer VID_W = VENUES > 1 ? $clog2(VENUES) : 1;

    typedef struct packed {
        logic known, live, submitted, acked, stop, unwind_failed;
        logic [1:0] cancel;
        logic [ACTION_W-1:0] cancel_action;
        logic [TAG_W-1:0] tag;
        logic [VENUE_W-1:0] venue;
        logic [LEASE_W-1:0] order_epoch;
        logic [ACCOUNT_W-1:0] account;
        logic [INSTRUMENT_W-1:0] instrument;
        logic sell;
        logic [GEN_W-1:0] generation;
        logic [REPORT_W-1:0] report_sequence;
        logic [EVENT_W-1:0] native_sequence;
        logic [PRICE_W-1:0] price;
        logic [QTY_W-1:0] leaves;
    } route_t;

    typedef struct packed {
        logic [1:0] source;
        logic [2:0] operation, report_kind;
        logic [TAG_W-1:0] tag;
        logic [VENUE_W-1:0] venue;
        logic [LEASE_W-1:0] order_epoch;
        logic [ACCOUNT_W-1:0] account;
        logic [INSTRUMENT_W-1:0] instrument;
        logic sell;
        logic [SLOT_W-1:0] slot;
        logic [GEN_W-1:0] generation;
        logic [REPORT_W-1:0] report_sequence;
        logic [EVENT_W-1:0] native_sequence;
        logic [PRICE_W-1:0] price;
        logic [QTY_W-1:0] qty;
        logic [MATCH_W-1:0] match_id;
        logic [ACTION_W-1:0] action_id;
    } work_t;

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
        logic [AMOUNT_W-1:0] reserved, open_amount, executed_amount;
        logic [COUNT_W-1:0] open_count;
    } receipt_t;

    typedef struct packed {
        logic [1:0] source;
        logic [4:0] status;
        logic [TAG_W-1:0] tag;
        logic [VENUE_W-1:0] venue;
        logic [LEASE_W-1:0] order_epoch;
        logic [SLOT_W-1:0] slot;
        logic [GEN_W-1:0] generation;
        logic [REPORT_W-1:0] report_sequence;
    } diagnostic_t;

    logic [1:0] state;
    logic audit_pending, egress_pending;
    work_t work_q, admit, settlement, withdrawal;
    diagnostic_t diagnostic;
    receipt_t native;
    // Different consumers project separate fields of the same slot record.
    /* verilator lint_off UNUSEDSIGNAL */
    route_t strategy_slot, report_slot, tx_route, candidate, revoked;
    /* verilator lint_on UNUSEDSIGNAL */
    wire [$bits(candidate)-1:0] route_image [0:ORDER_SLOTS-1];
    wire [VENUES-1:0] installed;
    wire [ORDER_SLOTS-1:0] new_ready, cancel_ready, unwind_due;
    wire [ORDER_SLOTS-1:0] kept_sequence, kept_exact, kept_execution;
    logic [SLOT_W-1:0] tx_cursor, unwind_cursor, next_tx, next_unwind;
    logic find_tx, find_unwind;
    wire raw_cfg_ready, raw_ctl_ready, core_ready, core_valid;
    wire [2:0] core_reason;
    wire [TOTAL_W-1:0] core_account_gross, core_account_turnover, core_firm_gross, core_firm_turnover;
    wire [COUNT_W-1:0] core_account_orders, core_firm_orders;
    wire [31:0] core_risk_epoch;

    wire s_slot_ok = int'(s_slot) < ORDER_SLOTS;
    wire r_slot_ok = int'(r_slot) < ORDER_SLOTS;
    wire s_venue_ok = int'(s_venue) < VENUES;
    wire r_venue_ok = int'(r_venue) < VENUES;
    wire [VID_W-1:0] strategy_venue = VENUES == 1 ? '0 : VID_W'(s_venue);
    wire [VID_W-1:0] egress_venue = VENUES == 1 ? '0 : VID_W'(tx_venue);
    assign strategy_slot = s_slot_ok ? route_image[s_slot] : '0;
    assign report_slot = r_slot_ok ? route_image[r_slot] : '0;
    assign tx_route = route_image[tx_slot];
    assign candidate = route_image[next_tx];
    assign revoked = route_image[next_unwind];

    logic [4:0] strategy_status, report_status;
    wire report_cancel = r_kind == R_CANCELED || r_kind == R_CANCEL_REJECT;
    wire report_terminal = r_kind == R_EXPIRED || r_kind == R_CANCELED || r_kind == R_REJECT ||
                          (r_kind == R_FILL && r_qty == report_slot.leaves);
    wire report_fault = report_status == REPORT_GAP || report_status == CHANGED_REPORT ||
                        report_status == REUSED_MATCH || report_status == LAST_SEQUENCE;

    assign m_valid = !rst && audit_pending;
    assign tx_valid = !rst && egress_pending;
    wire front_room = !rst && state == IDLE && (!audit_pending || m_ready) && !ctl_m_valid;
    wire choose_report = front_room && !find_unwind;
    wire choose_cfg = choose_report && !r_valid;
    wire choose_ctl = choose_cfg && !cfg_valid;
    assign r_ready = choose_report;
    assign cfg_ready = choose_cfg && raw_cfg_ready;
    assign ctl_ready = choose_ctl && raw_ctl_ready;
    assign s_ready = choose_ctl && !ctl_valid;

    wire strategy_fire = s_valid && s_ready;
    wire report_fire = r_valid && r_ready;
    wire risk_done = state == AWAIT && core_valid;
    wire risk_commit = risk_done && native.code == 0;
    wire unwind_failure = risk_done && work_q.source == RECOVERY && !risk_commit;
    wire report_committed = risk_commit && work_q.source == REPORT;
    wire new_committed = risk_commit && work_q.source == STRATEGY;
    wire report_updates_locally = report_fire &&
                                  (report_status == ENTRY_ACKED || report_status == CANCEL_REFUSED);
    wire local_cancel = strategy_fire && strategy_status == CANCEL_QUEUED;
    wire immediate_stop = local_cancel && s_slot == tx_slot && !tx_route.submitted;
    wire retire_now = risk_commit && work_q.slot == tx_slot && !native.live;

    wire lease_trip = (report_fire && report_fault) || (risk_done && work_q.source == REPORT && !risk_commit);
    wire [VENUE_W-1:0] fault_venue = report_fire ? r_venue : work_q.venue;

    risk_venue_lease #(.VENUES(VENUES), .LEASE_W(LEASE_W), .TIMER_W(TIMER_W)) leases (
        .clk, .rst, .time_tick, .link_loss, .trip(lease_trip), .trip_venue(fault_venue),
        .heartbeat_valid, .heartbeat_ready, .heartbeat_venue, .heartbeat_epoch,
        .g_valid, .g_ready, .g_op, .g_venue, .g_epoch, .g_timeout,
        .g_m_valid, .g_m_ready, .g_m_status, .g_m_op, .g_m_venue, .g_m_epoch, .g_m_countdown, .g_m_active,
        .allowed(venue_allowed), .installed, .effective_epochs(venue_epochs),
        .ignored_heartbeats, .lease_expirations
    );

    function automatic logic [LEASE_W-1:0] epoch_for(
        input logic [VENUES*LEASE_W-1:0] epochs,
        input logic [VENUE_W-1:0] venue
    );
        epoch_for = epochs[LEASE_W*venue +: LEASE_W];
    endfunction

    assign tx_applicable = tx_valid && tx_route.live && tx_route.generation == tx_generation &&
        venue_allowed[egress_venue] && tx_session_epoch == epoch_for(venue_epochs,tx_venue) &&
        !retire_now && (tx_cancel ? (tx_route.submitted && tx_route.cancel == QUEUED &&
                        tx_action == tx_route.cancel_action + 1'b1) :
                        (!tx_route.submitted && !tx_route.stop && !immediate_stop && !halted && !handoff_fault));
    wire tx_commit = tx_applicable && tx_ready;

    always @* begin
        strategy_status = APPLIED;
        if (!s_slot_ok || !s_venue_ok || int'(s_account) >= NUM_ACCOUNTS ||
            int'(s_instrument) >= NUM_INSTRUMENTS)
            strategy_status = BAD_ADDRESS;
        else if (s_cancel) begin
            if (s_price != 0 || s_qty != 0)
                strategy_status = BAD_FIELDS;
            else if (!strategy_slot.live || {s_venue,s_order_epoch,s_account,s_instrument,
                     s_sell,s_generation} != {strategy_slot.venue,strategy_slot.order_epoch,
                     strategy_slot.account,strategy_slot.instrument,strategy_slot.sell,
                     strategy_slot.generation})
                strategy_status = WRONG_ORDER;
            else if (strategy_slot.stop || strategy_slot.cancel != NONE)
                strategy_status = WRONG_PHASE;
            else if (strategy_slot.submitted && (&strategy_slot.cancel_action))
                strategy_status = ACTION_EXHAUSTED;
            else
                strategy_status = CANCEL_QUEUED;
        end else if (s_generation != 0 || s_price == 0 || s_qty == 0)
            strategy_status = BAD_FIELDS;
        else if (!venue_allowed[strategy_venue] || s_order_epoch != epoch_for(venue_epochs,s_venue) || halted || handoff_fault)
            strategy_status = LEASE_CLOSED;
    end

    always @* begin
        report_status = APPLIED;
        if (!r_slot_ok || !r_venue_ok)
            report_status = BAD_ADDRESS;
        else if (r_kind > R_EXPIRED || r_sequence == 0 || report_cancel != (r_action != 0) ||
                 (r_kind == R_FILL ? (r_price == 0 || r_qty == 0 || r_match == 0) :
                                      (r_price != 0 || r_qty != 0 || r_match != 0)))
            report_status = BAD_FIELDS;
        else if (!report_slot.known || {r_venue,r_order_epoch,r_generation} !=
                                       {report_slot.venue,report_slot.order_epoch,report_slot.generation})
            report_status = WRONG_ORDER;
        else if (!report_slot.submitted)
            report_status = UNSENT;
        else if (kept_sequence[r_slot])
            report_status = kept_exact[r_slot] ? REPLAY : CHANGED_REPORT;
        else if (r_sequence <= report_slot.report_sequence)
            report_status = OLD_REPORT;
        else if (!report_slot.live)
            report_status = WRONG_PHASE;
        else if (r_sequence != report_slot.report_sequence + 1'b1)
            report_status = REPORT_GAP;
        else if (r_kind == R_FILL && kept_execution[r_slot])
            report_status = REUSED_MATCH;
        else if ((&r_sequence) && !report_terminal)
            report_status = LAST_SEQUENCE;
        else if (((r_kind == R_ACK || r_kind == R_REJECT) && report_slot.acked) ||
                 (report_cancel && report_slot.cancel != AWAIT_CANCEL))
            report_status = WRONG_PHASE;
        else if (report_cancel && r_action != report_slot.cancel_action)
            report_status = WRONG_CANCEL_ACTION;
        else if (r_kind == R_ACK)
            report_status = ENTRY_ACKED;
        else if (r_kind == R_CANCEL_REJECT)
            report_status = CANCEL_REFUSED;
    end

    always @* begin
        admit = '0;
        admit.source = STRATEGY;
        admit.operation = N_NEW;
        admit.tag = s_tag;
        admit.venue = s_venue;
        admit.order_epoch = s_order_epoch;
        admit.account = s_account;
        admit.instrument = s_instrument;
        admit.sell = s_sell;
        admit.slot = s_slot;
        admit.generation = s_generation;
        admit.price = s_price;
        admit.qty = s_qty;

        settlement = '0;
        settlement.source = REPORT;
        settlement.operation = r_kind == R_FILL ? N_FILL : r_kind == R_REJECT ? N_REJECT : N_CANCEL;
        settlement.report_kind = r_kind;
        settlement.tag = r_tag;
        settlement.venue = r_venue;
        settlement.order_epoch = r_order_epoch;
        settlement.account = report_slot.account;
        settlement.instrument = report_slot.instrument;
        settlement.sell = report_slot.sell;
        settlement.slot = r_slot;
        settlement.generation = r_generation;
        settlement.report_sequence = r_sequence;
        settlement.native_sequence = report_slot.native_sequence + 1'b1;
        settlement.price = r_price;
        settlement.qty = r_qty;
        settlement.match_id = r_match;
        settlement.action_id = r_action;

        withdrawal = '0;
        withdrawal.source = RECOVERY;
        withdrawal.operation = N_CANCEL;
        withdrawal.tag = revoked.tag;
        withdrawal.venue = revoked.venue;
        withdrawal.order_epoch = revoked.order_epoch;
        withdrawal.account = revoked.account;
        withdrawal.instrument = revoked.instrument;
        withdrawal.sell = revoked.sell;
        withdrawal.slot = next_unwind;
        withdrawal.generation = revoked.generation;
        withdrawal.native_sequence = revoked.native_sequence + 1'b1;
    end

    wire [2:0] history_kind = state == AWAIT ? work_q.report_kind : r_kind;
    wire [REPORT_W-1:0] history_sequence = state == AWAIT ? work_q.report_sequence : r_sequence;
    wire [PRICE_W-1:0] history_price = state == AWAIT ? work_q.price : r_price;
    wire [QTY_W-1:0] history_qty = state == AWAIT ? work_q.qty : r_qty;
    wire [MATCH_W-1:0] history_match = state == AWAIT ? work_q.match_id : r_match;
    wire [ACTION_W-1:0] history_action = state == AWAIT ? work_q.action_id : r_action;
    for (genvar sl = 0; sl < ORDER_SLOTS; sl++) begin : g_slots
        localparam logic [SLOT_W-1:0] SLOT = SLOT_W'(sl);
        route_t saved, changed;
        wire [VID_W-1:0] venue_index = VENUES == 1 ? '0 : VID_W'(saved.venue);
        wire matching_lease = venue_allowed[venue_index] && saved.order_epoch == epoch_for(venue_epochs,saved.venue);
        wire abandoned = halted || handoff_fault || saved.stop || !installed[venue_index] ||
                         saved.order_epoch != epoch_for(venue_epochs,saved.venue);
        wire tx_applied_here = tx_commit && tx_slot == SLOT;
        wire strategy_cancel_here = local_cancel && s_slot == SLOT;
        wire local_report_here = report_updates_locally && r_slot == SLOT;
        wire risk_committed_here = risk_commit && work_q.slot == SLOT;

        assign route_image[sl] = saved;
        assign new_ready[sl] = saved.live && !saved.submitted && !saved.stop && matching_lease && !halted && !handoff_fault;
        assign cancel_ready[sl] = saved.live && saved.submitted && saved.cancel == QUEUED &&
                                  venue_allowed[venue_index];
        assign unwind_due[sl] = saved.live && !saved.submitted && !saved.unwind_failed && abandoned;

        risk_venue_history #(
            .DEPTH(HISTORY), .SEQ_W(REPORT_W), .PRICE_W(PRICE_W), .QTY_W(QTY_W), .MATCH_W(MATCH_W),
            .ACTION_W(ACTION_W)
        ) evidence (
            .clk, .rst, .replace_generation(new_committed && work_q.slot == SLOT),
            .append(local_report_here || (report_committed && work_q.slot == SLOT)),
            .kind(history_kind), .sequence_number(history_sequence), .price(history_price),
            .quantity(history_qty), .match_id(history_match), .action_id(history_action),
            .sequence_seen(kept_sequence[sl]),
            .exact_replay(kept_exact[sl]), .execution_seen(kept_execution[sl])
        );

        always @* begin
            changed = saved;
            if (tx_applied_here) begin
                if (tx_cancel) begin
                    changed.cancel = AWAIT_CANCEL;
                    changed.cancel_action = tx_action;
                end else
                    changed.submitted = 1'b1;
            end
            if (strategy_cancel_here) begin
                if (saved.submitted)
                    changed.cancel = QUEUED;
                else
                    changed.stop = 1'b1;
            end
            if (local_report_here) begin
                changed.report_sequence = r_sequence;
                if (r_kind == R_ACK)
                    changed.acked = 1'b1;
                else
                    changed.cancel = NONE;
            end
            if (unwind_failure && work_q.slot == SLOT)
                changed.unwind_failed = 1'b1;
            if (risk_committed_here) begin
                if (new_committed) begin
                    changed = '0;
                    changed.known = 1'b1;
                    changed.tag = work_q.tag;
                    changed.venue = work_q.venue;
                    changed.order_epoch = work_q.order_epoch;
                    changed.account = work_q.account;
                    changed.instrument = work_q.instrument;
                    changed.sell = work_q.sell;
                end
                changed.generation = native.generation;
                changed.live = native.live;
                changed.native_sequence = native.event_seq;
                changed.price = native.price;
                changed.leaves = native.leaves;
                if (report_committed)
                    changed.report_sequence = work_q.report_sequence;
                if (!native.live)
                    changed.cancel = NONE;
            end
        end

        always_ff @(posedge clk) begin
            if (rst)
                saved <= '0;
            else
                saved <= changed;
        end
    end

    function automatic logic [SLOT_W-1:0] step_slot(input logic [SLOT_W-1:0] slot);
        step_slot = slot == SLOT_W'(ORDER_SLOTS-1) ? '0 : slot + 1'b1;
    endfunction

    // Cancellations have priority on egress. Both scans rotate after taking
    // work so a held offer or frequent use of one slot cannot bias selection.
    always @* begin : find_work
        integer tx_idx, unwind_idx;
        find_tx = 1'b0;
        find_unwind = 1'b0;
        next_tx = '0;
        next_unwind = '0;
        for (integer offset = 0; offset < ORDER_SLOTS; offset++) begin
            tx_idx = int'(tx_cursor) + offset;
            if (tx_idx >= ORDER_SLOTS)
                tx_idx -= ORDER_SLOTS;
            unwind_idx = int'(unwind_cursor) + offset;
            if (unwind_idx >= ORDER_SLOTS)
                unwind_idx -= ORDER_SLOTS;
            if (!find_tx && ((|cancel_ready) ? cancel_ready[tx_idx] : new_ready[tx_idx])) begin
                find_tx = 1'b1;
                next_tx = SLOT_W'(tx_idx);
            end
            if (!find_unwind && unwind_due[unwind_idx]) begin
                find_unwind = 1'b1;
                next_unwind = SLOT_W'(unwind_idx);
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            egress_pending <= 1'b0;
            tx_cursor <= '0;
            {tx_cancel,tx_order_tag,tx_venue,tx_order_epoch,tx_session_epoch,tx_account,
             tx_instrument,tx_sell,tx_slot,tx_generation,tx_action,tx_price,tx_qty} <= '0;
        end else if (egress_pending) begin
            if (tx_ready)
                egress_pending <= 1'b0;
        end else if (find_tx) begin
            egress_pending <= 1'b1;
            tx_cursor <= step_slot(next_tx);
            tx_cancel <= |cancel_ready;
            tx_order_tag <= candidate.tag;
            tx_venue <= candidate.venue;
            tx_order_epoch <= candidate.order_epoch;
            tx_session_epoch <= epoch_for(venue_epochs,candidate.venue);
            tx_account <= candidate.account;
            tx_instrument <= candidate.instrument;
            tx_sell <= candidate.sell;
            tx_slot <= next_tx;
            tx_generation <= candidate.generation;
            tx_action <= (|cancel_ready) ? candidate.cancel_action + 1'b1 : '0;
            tx_price <= (|cancel_ready) ? '0 : candidate.price;
            tx_qty <= (|cancel_ready) ? '0 : candidate.leaves;
        end
    end

    function automatic diagnostic_t describe(
        /* verilator lint_off UNUSEDSIGNAL */
        input work_t command,
        /* verilator lint_on UNUSEDSIGNAL */
        input logic [4:0] status,
        input logic [GEN_W-1:0] generation
    );
        describe = {command.source,status,command.tag,command.venue,command.order_epoch,
                    command.slot,generation,command.report_sequence};
    endfunction

    wire local_result = (report_fire && report_status != APPLIED) ||
                        (strategy_fire && strategy_status != APPLIED);
    always @* begin
        if (risk_done)
            diagnostic = describe(work_q, !risk_commit ? RISK_REFUSED :
                work_q.source == RECOVERY ? ROLLED_BACK : APPLIED,
                risk_commit ? native.generation : work_q.generation);
        else if (report_fire)
            diagnostic = describe(settlement, report_status, settlement.generation);
        else
            diagnostic = describe(admit, strategy_status, admit.generation);
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= IDLE;
            work_q <= '0;
            handoff_fault <= 1'b0;
            audit_pending <= 1'b0;
            unwind_cursor <= '0;
            {m_source,m_status,m_tag,m_venue,m_order_epoch,m_slot,m_generation,m_sequence} <= '0;
            {m_native_valid,m_native,m_portfolio_reason,m_account_gross,m_account_turnover,
             m_account_orders,m_firm_gross,m_firm_turnover,m_firm_orders,m_risk_epoch} <= '0;
        end else begin
            if (unwind_failure)
                handoff_fault <= 1'b1;
            if (m_ready)
                audit_pending <= 1'b0;
            if (local_result || risk_done) begin
                audit_pending <= 1'b1;
                {m_source,m_status,m_tag,m_venue,m_order_epoch,m_slot,m_generation,m_sequence} <= diagnostic;
                m_native_valid <= risk_done;
                m_native <= risk_done ? native : '0;
                m_portfolio_reason <= risk_done ? core_reason : '0;
                m_account_gross <= risk_done ? core_account_gross : '0;
                m_account_turnover <= risk_done ? core_account_turnover : '0;
                m_account_orders <= risk_done ? core_account_orders : '0;
                m_firm_gross <= risk_done ? core_firm_gross : '0;
                m_firm_turnover <= risk_done ? core_firm_turnover : '0;
                m_firm_orders <= risk_done ? core_firm_orders : '0;
                m_risk_epoch <= risk_done ? core_risk_epoch : '0;
            end

            case (state)
                IDLE: if (front_room) begin
                    if (find_unwind) begin
                        work_q <= withdrawal;
                        unwind_cursor <= step_slot(next_unwind);
                        state <= ISSUE;
                    end else if (report_fire && report_status == APPLIED) begin
                        work_q <= settlement;
                        state <= ISSUE;
                    end else if (strategy_fire && strategy_status == APPLIED) begin
                        work_q <= admit;
                        state <= ISSUE;
                    end
                end
                ISSUE: if (core_ready)
                    state <= AWAIT;
                AWAIT: if (core_valid)
                    state <= IDLE;
                default: state <= IDLE;
            endcase
        end
    end

    risk_portfolio_gateway #(
        .NUM_ACCOUNTS(NUM_ACCOUNTS), .NUM_INSTRUMENTS(NUM_INSTRUMENTS), .ORDER_SLOTS(ORDER_SLOTS),
        .TAG_W(TAG_W), .PRICE_W(PRICE_W), .QTY_W(QTY_W), .GEN_W(GEN_W), .EVENT_W(EVENT_W)
    ) reservation (
        .clk, .rst, .halt_req, .halted,
        .cfg_valid(cfg_valid && choose_cfg), .cfg_ready(raw_cfg_ready), .cfg_account, .cfg_instrument,
        .cfg_sell, .cfg_enable, .cfg_min_price, .cfg_max_price, .cfg_max_order_qty,
        .cfg_max_open_notional, .cfg_max_open_orders,
        .ctl_valid(ctl_valid && choose_ctl), .ctl_ready(raw_ctl_ready), .ctl_op, .ctl_firm, .ctl_account,
        .ctl_mask, .ctl_gross_limit, .ctl_count_limit, .ctl_usage_limit, .ctl_epoch,
        .ctl_m_valid, .ctl_m_ready, .ctl_m_status, .ctl_m_mask, .ctl_m_gross_limit, .ctl_m_count_limit,
        .ctl_m_usage_limit, .ctl_m_gross, .ctl_m_turnover, .ctl_m_orders, .ctl_m_epoch,
        .s_valid(state == ISSUE), .s_ready(core_ready), .s_op(work_q.operation), .s_tag(work_q.tag),
        .s_account(work_q.account), .s_instrument(work_q.instrument), .s_sell(work_q.sell),
        .s_slot(work_q.slot), .s_generation(work_q.generation), .s_event_seq(work_q.native_sequence),
        .s_price(work_q.price), .s_qty(work_q.qty),
        .m_valid(core_valid), .m_ready(state == AWAIT), .m_native(native),
        .m_portfolio_reason(core_reason), .m_account_gross(core_account_gross),
        .m_account_turnover(core_account_turnover), .m_account_orders(core_account_orders),
        .m_firm_gross(core_firm_gross), .m_firm_turnover(core_firm_turnover),
        .m_firm_orders(core_firm_orders), .m_epoch(core_risk_epoch)
    );

`ifdef FORMAL
    always_ff @(posedge clk) begin
        if (!rst && !$past(rst)) begin
            if ($past(tx_valid && !tx_ready && !tx_applicable))
                assert(!tx_applicable);
            if ($past(tx_valid && !tx_ready))
                assert(tx_valid && $stable({tx_cancel,tx_order_tag,tx_venue,tx_order_epoch,
                       tx_session_epoch,tx_account,tx_instrument,tx_sell,tx_slot,tx_generation,tx_action,tx_price,tx_qty}));
            if ($past(m_valid && !m_ready))
                assert(m_valid && $stable({m_source,m_status,m_tag,m_venue,m_order_epoch,m_slot,
                       m_generation,m_sequence,m_native_valid,m_native,m_portfolio_reason,m_account_gross,
                       m_account_turnover,m_account_orders,m_firm_gross,m_firm_turnover,m_firm_orders,m_risk_epoch}));
            if (tx_commit && !tx_cancel)
                assert(!halted && tx_route.live && !tx_route.submitted);
            if (risk_commit && work_q.source == RECOVERY)
                assert(work_q.operation == N_CANCEL && !native.live);
            if ($past(handoff_fault))
                assert(handoff_fault);
        end
    end
`endif
endmodule

`default_nettype wire
