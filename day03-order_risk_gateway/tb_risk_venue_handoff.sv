`timescale 1ns/1ps
`default_nettype none

module tb_risk_venue_handoff #(
    parameter integer NUM_ACCOUNTS = 3,
    parameter integer NUM_INSTRUMENTS = 3,
    parameter integer ORDER_SLOTS = 5,
    parameter integer VENUES = 3,
    parameter integer TAG_W = 16,
    parameter integer PRICE_W = 8,
    parameter integer QTY_W = 8,
    parameter integer GEN_W = 4,
    parameter integer EVENT_W = 5,
    parameter integer LEASE_W = 8,
    parameter integer TIMER_W = 8,
    parameter integer REPORT_W = 4,
    parameter integer MATCH_W = 16,
    parameter integer HISTORY = 3,
    parameter integer ACTION_W = 3
);
    localparam integer ACCOUNT_W = NUM_ACCOUNTS > 1 ? $clog2(NUM_ACCOUNTS) : 1;
    localparam integer INSTRUMENT_W = NUM_INSTRUMENTS > 1 ? $clog2(NUM_INSTRUMENTS) : 1;
    localparam integer SLOT_W = ORDER_SLOTS > 1 ? $clog2(ORDER_SLOTS) : 1;
    localparam integer VENUE_W = $clog2(VENUES+1);
    localparam integer COUNT_W = $clog2(ORDER_SLOTS+1);
    localparam integer AMOUNT_W = PRICE_W + QTY_W;
    localparam integer TOTAL_W = AMOUNT_W + $clog2(ORDER_SLOTS+2*NUM_ACCOUNTS*NUM_INSTRUMENTS+1);
    localparam integer NATIVE_W = 3+TAG_W+ACCOUNT_W+INSTRUMENT_W+1+SLOT_W+GEN_W+EVENT_W+5+1+
                                 QTY_W+PRICE_W+3*AMOUNT_W+COUNT_W;
    localparam integer TX_W = 2+TAG_W+VENUE_W+2*LEASE_W+ACCOUNT_W+INSTRUMENT_W+SLOT_W+GEN_W+PRICE_W+QTY_W+ACTION_W;
    localparam integer AUDIT_W = 2+5+TAG_W+VENUE_W+LEASE_W+SLOT_W+GEN_W+REPORT_W+1+NATIVE_W+3+
                                 4*TOTAL_W+2*COUNT_W+32;
    localparam integer CONTROL_W = 6+4*TOTAL_W+2*COUNT_W+32;
    localparam integer MANAGEMENT_W = 6+VENUE_W+LEASE_W+TIMER_W;
    localparam integer RESET = 0, CONFIG = 1, CONTROL = 2, LEASE = 3, INTENT = 4,
        REPORT_IN = 5, TAKE = 6, LOSS = 7, TICKS = 8, HEARTBEAT = 9, HALT = 10,
        WAIT_FOR = 11, WAIT_TX = 12, CANCEL_RACE = 13, PIN_OBSERVE = 14, EXPIRE_RACE = 15, CORRUPT_LEDGER = 16;

    logic clk = 0;
    always #5 clk = !clk;

    logic rst = 1, halt_req = 0;
    wire halted, handoff_fault;
    logic cfg_valid = 0, cfg_sell, cfg_enable;
    wire cfg_ready;
    logic [ACCOUNT_W-1:0] cfg_account;
    logic [INSTRUMENT_W-1:0] cfg_instrument;
    logic [PRICE_W-1:0] cfg_min_price, cfg_max_price;
    logic [QTY_W-1:0] cfg_max_order_qty;
    logic [AMOUNT_W-1:0] cfg_max_open_notional;
    logic [COUNT_W-1:0] cfg_max_open_orders;

    logic ctl_valid = 0, ctl_firm;
    wire ctl_ready, ctl_m_valid, ctl_m_ready;
    logic [1:0] ctl_op;
    logic [ACCOUNT_W-1:0] ctl_account;
    logic [2:0] ctl_mask;
    logic [TOTAL_W-1:0] ctl_gross_limit, ctl_usage_limit;
    logic [COUNT_W-1:0] ctl_count_limit;
    logic [31:0] ctl_epoch;
    wire [2:0] ctl_m_status, ctl_m_mask;
    wire [TOTAL_W-1:0] ctl_m_gross_limit, ctl_m_usage_limit, ctl_m_gross, ctl_m_turnover;
    wire [COUNT_W-1:0] ctl_m_count_limit, ctl_m_orders;
    wire [31:0] ctl_m_epoch;

    logic time_tick = 0, heartbeat_valid = 0;
    logic [VENUES-1:0] link_loss = '0;
    wire heartbeat_ready;
    logic [VENUE_W-1:0] heartbeat_venue, g_venue;
    logic [LEASE_W-1:0] heartbeat_epoch, g_epoch;
    logic g_valid = 0;
    wire g_ready, g_m_valid, g_m_ready;
    logic [1:0] g_op;
    logic [TIMER_W-1:0] g_timeout;
    wire [2:0] g_m_status;
    wire [1:0] g_m_op;
    wire [VENUE_W-1:0] g_m_venue;
    wire [LEASE_W-1:0] g_m_epoch;
    wire [TIMER_W-1:0] g_m_countdown;
    wire g_m_active;
    wire [VENUES-1:0] venue_allowed;
    wire [VENUES*LEASE_W-1:0] venue_epochs;
    wire [31:0] ignored_heartbeats, lease_expirations;

    logic s_valid = 0, s_cancel;
    wire s_ready;
    logic [TAG_W-1:0] s_tag, r_tag;
    logic [VENUE_W-1:0] s_venue, r_venue;
    logic [LEASE_W-1:0] s_order_epoch, r_order_epoch;
    logic [ACCOUNT_W-1:0] s_account;
    logic [INSTRUMENT_W-1:0] s_instrument;
    logic s_sell;
    logic [SLOT_W-1:0] s_slot, r_slot;
    logic [GEN_W-1:0] s_generation, r_generation;
    logic [PRICE_W-1:0] s_price, r_price;
    logic [QTY_W-1:0] s_qty, r_qty;
    logic r_valid = 0;
    wire r_ready;
    logic [2:0] r_kind;
    logic [REPORT_W-1:0] r_sequence;
    logic [MATCH_W-1:0] r_match;
    logic [ACTION_W-1:0] r_action;

    wire tx_valid, tx_applicable, tx_cancel;
    logic tx_ready = 0;
    wire [TAG_W-1:0] tx_order_tag;
    wire [VENUE_W-1:0] tx_venue;
    wire [LEASE_W-1:0] tx_order_epoch, tx_session_epoch;
    wire [ACCOUNT_W-1:0] tx_account;
    wire [INSTRUMENT_W-1:0] tx_instrument;
    wire tx_sell;
    wire [SLOT_W-1:0] tx_slot;
    wire [GEN_W-1:0] tx_generation;
    wire [ACTION_W-1:0] tx_action;
    wire [PRICE_W-1:0] tx_price;
    wire [QTY_W-1:0] tx_qty;

    wire m_valid, m_ready, m_native_valid;
    wire [1:0] m_source;
    wire [4:0] m_status;
    wire [TAG_W-1:0] m_tag;
    wire [VENUE_W-1:0] m_venue;
    wire [LEASE_W-1:0] m_order_epoch;
    wire [SLOT_W-1:0] m_slot;
    wire [GEN_W-1:0] m_generation;
    wire [REPORT_W-1:0] m_sequence;
    wire [NATIVE_W-1:0] m_native;
    wire [2:0] m_portfolio_reason;
    wire [TOTAL_W-1:0] m_account_gross, m_account_turnover, m_firm_gross, m_firm_turnover;
    wire [COUNT_W-1:0] m_account_orders, m_firm_orders;
    wire [31:0] m_risk_epoch;

    risk_venue_handoff #(
        .NUM_ACCOUNTS(NUM_ACCOUNTS), .NUM_INSTRUMENTS(NUM_INSTRUMENTS), .ORDER_SLOTS(ORDER_SLOTS),
        .VENUES(VENUES), .TAG_W(TAG_W), .PRICE_W(PRICE_W), .QTY_W(QTY_W),
        .GEN_W(GEN_W), .EVENT_W(EVENT_W), .LEASE_W(LEASE_W), .TIMER_W(TIMER_W),
        .REPORT_W(REPORT_W), .MATCH_W(MATCH_W), .HISTORY(HISTORY), .ACTION_W(ACTION_W)
    ) dut (.*);

    wire [TX_W-1:0] egress_payload = {tx_cancel,tx_order_tag,tx_venue,tx_order_epoch,
        tx_session_epoch,tx_account,tx_instrument,tx_sell,tx_slot,tx_generation,tx_action,tx_price,tx_qty};
    wire [AUDIT_W-1:0] audit_payload = {m_source,m_status,m_tag,m_venue,m_order_epoch,m_slot,
        m_generation,m_sequence,m_native_valid,m_native,m_portfolio_reason,m_account_gross,
        m_account_turnover,m_account_orders,m_firm_gross,m_firm_turnover,m_firm_orders,m_risk_epoch};
    wire [CONTROL_W-1:0] control_payload = {ctl_m_status,ctl_m_mask,ctl_m_gross_limit,
        ctl_m_count_limit,ctl_m_usage_limit,ctl_m_gross,ctl_m_turnover,ctl_m_orders,ctl_m_epoch};
    wire [MANAGEMENT_W-1:0] management_payload = {g_m_status,g_m_op,g_m_venue,g_m_epoch,
        g_m_countdown,g_m_active};

    integer ticks_seen = 0;
    integer audit_count = 0, management_count = 0, control_count = 0, egress_count = 0;
    integer audit_stalls = 0, egress_stalls = 0, canceled_offers = 0;
    logic [TX_W-1:0] kept_egress;
    logic [AUDIT_W-1:0] kept_audit;
    logic [CONTROL_W-1:0] kept_control;
    logic [MANAGEMENT_W-1:0] kept_management;
    logic hold_tx = 0, hold_audit = 0, hold_management = 0, hold_control = 0;
    logic waived = 0;
    assign m_ready = (ticks_seen % 9) < 6;
    assign ctl_m_ready = (ticks_seen % 5) < 2;
    assign g_m_ready = (ticks_seen % 7) < 3;

    string vector_dir, wave_path;
    integer stimulus, audit_output, management_output, controls_output, egress_output, pins_output;
    integer op, used, row_number = 0;
    logic [127:0] row [0:10];

    always @(posedge clk) begin
        ticks_seen <= ticks_seen + 1;
        if (rst) begin
            hold_tx <= 0;
            hold_audit <= 0;
            hold_management <= 0;
            hold_control <= 0;
            waived <= 0;
        end else begin
            if (hold_tx && (!tx_valid || egress_payload !== kept_egress))
                $fatal(1,"egress changed under stall");
            if (hold_tx && waived && tx_applicable)
                $fatal(1,"a revoked egress offer revived");
            if (hold_audit && (!m_valid || audit_payload !== kept_audit))
                $fatal(1,"audit changed under stall");
            if (hold_management && (!g_m_valid || management_payload !== kept_management))
                $fatal(1,"lease result changed under stall");
            if (hold_control && (!ctl_m_valid || control_payload !== kept_control))
                $fatal(1,"portfolio control result changed under stall");

            hold_tx <= tx_valid && !tx_ready;
            hold_audit <= m_valid && !m_ready;
            hold_management <= g_m_valid && !g_m_ready;
            hold_control <= ctl_m_valid && !ctl_m_ready;
            waived <= tx_valid && !tx_ready && !tx_applicable;
            kept_egress <= egress_payload;
            kept_audit <= audit_payload;
            kept_control <= control_payload;
            kept_management <= management_payload;
            if (tx_valid && !tx_ready)
                egress_stalls <= egress_stalls + 1;
            if (m_valid && !m_ready)
                audit_stalls <= audit_stalls + 1;

            if (m_valid && m_ready) begin
                audit_count <= audit_count + 1;
                $fdisplay(audit_output,
                    "%h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h",
                    m_source,m_status,m_tag,m_venue,m_order_epoch,m_slot,m_generation,m_sequence,
                    m_native_valid,m_native,m_portfolio_reason,m_account_gross,m_account_turnover,
                    m_account_orders,m_firm_gross,m_firm_turnover,m_firm_orders,m_risk_epoch);
            end
            if (g_m_valid && g_m_ready) begin
                management_count <= management_count + 1;
                $fdisplay(management_output, "%h %h %h %h %h %h",
                    g_m_status,g_m_op,g_m_venue,g_m_epoch,g_m_countdown,g_m_active);
            end
            if (ctl_m_valid && ctl_m_ready) begin
                control_count <= control_count + 1;
                $fdisplay(controls_output,"%h %h %h %h %h %h %h %h %h",
                    ctl_m_status,ctl_m_mask,ctl_m_gross_limit,ctl_m_count_limit,
                    ctl_m_usage_limit,ctl_m_gross,ctl_m_turnover,ctl_m_orders,ctl_m_epoch);
            end
            if (tx_valid && tx_ready) begin
                egress_count <= egress_count + 1;
                if (!tx_applicable)
                    canceled_offers <= canceled_offers + 1;
                $fdisplay(egress_output,"%h %h %h %h %h %h %h %h %h %h %h %h %h %h",
                    tx_applicable,tx_cancel,tx_order_tag,tx_venue,tx_order_epoch,tx_session_epoch,
                    tx_account,tx_instrument,tx_sell,tx_slot,tx_generation,tx_action,tx_price,tx_qty);
            end
        end
    end

    task automatic drive_intent(input bit cancel_race);
        s_cancel = 1'(row[0]);
        s_tag = TAG_W'(row[1]);
        s_venue = VENUE_W'(row[2]);
        s_order_epoch = LEASE_W'(row[3]);
        s_account = ACCOUNT_W'(row[4]);
        s_instrument = INSTRUMENT_W'(row[5]);
        s_sell = 1'(row[6]);
        s_slot = SLOT_W'(row[7]);
        s_generation = GEN_W'(row[8]);
        s_price = PRICE_W'(row[9]);
        s_qty = QTY_W'(row[10]);
        s_valid = 1;
        if (cancel_race) begin
            #1;
            if (!s_ready || !tx_valid)
                $fatal(1,"cancel race lacks ready intent and held offer");
            tx_ready = 1;
        end
        do @(posedge clk); while (!s_ready);
        @(negedge clk);
        s_valid = 0;
        tx_ready = 0;
    endtask

    initial begin : driver
        if (!$value$plusargs("vector_dir=%s", vector_dir))
            $fatal(1,"missing vector_dir argument");
        if ($value$plusargs("wave=%s", wave_path)) begin
            $dumpfile(wave_path);
            $dumpvars(1,tb_risk_venue_handoff);
        end
        stimulus = $fopen({vector_dir,"/stimulus.hex"},"r");
        audit_output = $fopen({vector_dir,"/actual-audit.hex"},"w");
        management_output = $fopen({vector_dir,"/actual-management.hex"},"w");
        controls_output = $fopen({vector_dir,"/actual-control.hex"},"w");
        egress_output = $fopen({vector_dir,"/actual-egress.hex"},"w");
        pins_output = $fopen({vector_dir,"/actual-pins.hex"},"w");
        if (!stimulus || !audit_output || !management_output || !controls_output ||
            !egress_output || !pins_output)
            $fatal(1,"could not open handoff fixture files");

        @(negedge clk);
        while (!$feof(stimulus)) begin
            used = $fscanf(stimulus,"%h %h %h %h %h %h %h %h %h %h %h %h\n",
                op,row[0],row[1],row[2],row[3],row[4],row[5],row[6],row[7],row[8],row[9],row[10]);
            if (used == -1 && $feof(stimulus))
                break;
            if (used != 12)
                $fatal(1,"bad handoff row %0d",row_number);
            row_number++;
            case (op)
                RESET: begin
                    rst = 1;
                    halt_req = 0;
                    link_loss = '0;
                    repeat (3) @(negedge clk);
                    rst = 0;
                    @(negedge clk);
                end
                CONFIG: begin
                    cfg_valid = 1;
                    cfg_account = ACCOUNT_W'(row[0]);
                    cfg_instrument = INSTRUMENT_W'(row[1]);
                    cfg_sell = 1'(row[2]);
                    cfg_enable = 1'(row[3]);
                    cfg_min_price = PRICE_W'(row[4]);
                    cfg_max_price = PRICE_W'(row[5]);
                    cfg_max_order_qty = QTY_W'(row[6]);
                    cfg_max_open_notional = AMOUNT_W'(row[7]);
                    cfg_max_open_orders = COUNT_W'(row[8]);
                    do @(posedge clk); while (!cfg_ready);
                    @(negedge clk);
                    cfg_valid = 0;
                end
                CONTROL: begin
                    ctl_valid = 1;
                    ctl_op = 2'(row[0]);
                    ctl_firm = 1'(row[1]);
                    ctl_account = ACCOUNT_W'(row[2]);
                    ctl_mask = 3'(row[3]);
                    ctl_gross_limit = TOTAL_W'(row[4]);
                    ctl_count_limit = COUNT_W'(row[5]);
                    ctl_usage_limit = TOTAL_W'(row[6]);
                    ctl_epoch = 32'(row[7]);
                    do @(posedge clk); while (!ctl_ready);
                    @(negedge clk);
                    ctl_valid = 0;
                end
                LEASE: begin
                    g_valid = 1;
                    g_op = 2'(row[0]);
                    g_venue = VENUE_W'(row[1]);
                    g_epoch = LEASE_W'(row[2]);
                    g_timeout = TIMER_W'(row[3]);
                    do @(posedge clk); while (!g_ready);
                    @(negedge clk);
                    g_valid = 0;
                end
                INTENT: drive_intent(0);
                CANCEL_RACE: drive_intent(1);
                REPORT_IN: begin
                    r_valid = 1;
                    r_kind = 3'(row[0]);
                    r_tag = TAG_W'(row[1]);
                    r_venue = VENUE_W'(row[2]);
                    r_order_epoch = LEASE_W'(row[3]);
                    r_slot = SLOT_W'(row[4]);
                    r_generation = GEN_W'(row[5]);
                    r_sequence = REPORT_W'(row[6]);
                    r_price = PRICE_W'(row[7]);
                    r_qty = QTY_W'(row[8]);
                    r_match = MATCH_W'(row[9]);
                    r_action = ACTION_W'(row[10]);
                    do @(posedge clk); while (!r_ready);
                    @(negedge clk);
                    r_valid = 0;
                end
                TAKE: begin
                    while (!tx_valid) @(negedge clk);
                    tx_ready = 1;
                    @(negedge clk);
                    tx_ready = 0;
                end
                LOSS: begin
                    link_loss = VENUES'(row[0]);
                    @(negedge clk);
                end
                TICKS: begin
                    time_tick = 1;
                    repeat (int'(row[0])) @(negedge clk);
                    time_tick = 0;
                end
                HEARTBEAT: begin
                    heartbeat_valid = 1;
                    heartbeat_venue = VENUE_W'(row[0]);
                    heartbeat_epoch = LEASE_W'(row[1]);
                    time_tick = 1'(row[2]);
                    @(posedge clk);
                    if (!heartbeat_ready)
                        $fatal(1,"watchdog failed to accept heartbeat");
                    @(negedge clk);
                    heartbeat_valid = 0;
                    time_tick = 0;
                end
                HALT: begin
                    halt_req = 1;
                    @(negedge clk);
                    halt_req = 0;
                end
                WAIT_FOR: begin
                    while (audit_count < int'(row[0]) || management_count < int'(row[1]) ||
                           control_count < int'(row[2]) || egress_count < int'(row[3]))
                        @(negedge clk);
                    repeat (int'(row[4])) @(negedge clk);
                end
                WAIT_TX: begin
                    while (!tx_valid) @(negedge clk);
                    #1;
                    if (tx_applicable !== 1'(row[0]))
                        $fatal(1,"unexpected tx qualification at row %0d",row_number);
                    @(negedge clk);
                end
                PIN_OBSERVE: begin
                    #1;
                    $fdisplay(pins_output,"%h %h %h %h %h %h",venue_allowed,venue_epochs,
                              ignored_heartbeats,lease_expirations,halted,handoff_fault);
                    @(negedge clk);
                end
                EXPIRE_RACE: begin
                    if (!tx_valid)
                        $fatal(1,"timeout race lacks held egress");
                    tx_ready = 1;
                    time_tick = 1;
                    @(negedge clk);
                    tx_ready = 0;
                    time_tick = 0;
                end
                CORRUPT_LEDGER: begin
                    // Change the captured read value without changing committed RAM.
                    if (row[0] != 0)
                        force dut.reservation.core.ledger_q = '0;
                    else
                        release dut.reservation.core.ledger_q;
                    @(negedge clk);
                end
                default: $fatal(1,"bad handoff opcode %0d",op);
            endcase
        end
        repeat (24) @(negedge clk);
        $fclose(stimulus);
        $fclose(audit_output);
        $fclose(management_output);
        $fclose(controls_output);
        $fclose(egress_output);
        $fclose(pins_output);
        if (!audit_stalls || !egress_stalls || !canceled_offers)
            $fatal(1,"fixture lacked blocked audit, blocked egress or waived egress");
        $display("PASS handoff transport and output holds: %0d audit stalls, %0d egress stalls, %0d revoked offers",
                 audit_stalls,egress_stalls,canceled_offers);
        $finish;
    end

    initial begin
        #10000000;
        $fatal(1,"handoff test timeout at row %0d",row_number);
    end
endmodule

`default_nettype wire
