`timescale 1ns/1ps
`default_nettype none

module tb_order_risk_gateway #(
    parameter integer PRICE_W = 8,
    parameter integer QTY_W = 8
);
    localparam integer ACCOUNT_W = 2;
    localparam integer INSTRUMENT_W = 2;
    localparam integer SLOT_W = 3;
    localparam integer COUNT_W = 3;
    localparam integer GEN_W = 3;
    localparam integer EVENT_W = 4;
    localparam integer TAG_W = 16;
    localparam integer NOTIONAL_W = PRICE_W + QTY_W;
    localparam integer RECORD_W = 3 + TAG_W + ACCOUNT_W + INSTRUMENT_W + 1 + SLOT_W +
                                  GEN_W + EVENT_W + 5 + 1 + QTY_W + PRICE_W +
                                  3 * NOTIONAL_W + COUNT_W;
    localparam integer FRAME_WORDS = (RECORD_W + 31) / 32 + 4;

    logic clk = 1'b0;
    always #5 clk = ~clk;

    logic rst, halt_req;
    wire halted;
    logic cfg_valid, cfg_sell, cfg_enable;
    wire cfg_ready;
    logic [ACCOUNT_W-1:0] cfg_account;
    logic [INSTRUMENT_W-1:0] cfg_instrument;
    logic [PRICE_W-1:0] cfg_min_price, cfg_max_price;
    logic [QTY_W-1:0] cfg_max_order_qty;
    logic [NOTIONAL_W-1:0] cfg_max_open_notional;
    logic [COUNT_W-1:0] cfg_max_open_orders;

    logic s_valid, s_sell;
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

    wire m_valid, m_ready, m_sell, m_slot_live;
    wire [2:0] m_op;
    wire [TAG_W-1:0] m_tag;
    wire [ACCOUNT_W-1:0] m_account;
    wire [INSTRUMENT_W-1:0] m_instrument;
    wire [SLOT_W-1:0] m_slot;
    wire [GEN_W-1:0] m_generation;
    wire [EVENT_W-1:0] m_event_seq;
    wire [4:0] m_code;
    wire [QTY_W-1:0] m_leaves;
    wire [PRICE_W-1:0] m_limit_price;
    wire [NOTIONAL_W-1:0] m_reserved, m_open_notional, m_executed_notional;
    wire [COUNT_W-1:0] m_open_orders;

`ifdef RISK_POST_SYNTH
    hft_order_risk_gateway dut (.*);
`else
    hft_order_risk_gateway #(
        .NUM_ACCOUNTS(3), .NUM_INSTRUMENTS(3), .ORDER_SLOTS(5),
        .TAG_W(TAG_W), .PRICE_W(PRICE_W), .QTY_W(QTY_W),
        .GEN_W(GEN_W), .EVENT_W(EVENT_W)
    ) dut (.*);
`endif

    logic audit_rst, sink_ready, stream_ready;
    wire audit_in_ready, stream_valid, stream_first, stream_last;
    wire decision_valid, audit_record_valid;
    wire [RECORD_W-1:0] outgoing_record, audit_record;
    wire [31:0] dropped;
    wire drop_saturated;
    wire [31:0] stream_data;
    wire [RECORD_W-1:0] record = {
        m_op, m_tag, m_account, m_instrument, m_sell, m_slot, m_generation,
        m_event_seq, m_code, m_slot_live, m_leaves, m_limit_price, m_reserved,
        m_open_notional, m_executed_notional, m_open_orders
    };
    risk_audit_tap #(.RECORD_BITS(RECORD_W), .AUDIT_DEPTH(3)) tap (
        .clk, .rst(audit_rst), .s_valid(m_valid), .s_ready(m_ready), .s_record(record),
        .m_valid(decision_valid), .m_ready(sink_ready), .m_record(outgoing_record),
        .a_valid(audit_record_valid), .a_ready(audit_in_ready), .a_record(audit_record),
        .dropped, .drop_saturated
    );

    risk_audit_serializer #(.PAYLOAD_BITS(RECORD_W)) audit (
        .clk, .rst(audit_rst),
        .s_valid(audit_record_valid), .s_ready(audit_in_ready), .s_payload(audit_record),
        .m_valid(stream_valid), .m_ready(stream_ready), .m_data(stream_data),
        .m_first(stream_first), .m_last(stream_last)
    );

    // Step the seeded random source used to vary bench handshakes.
    function automatic logic [31:0] advance_random(input logic [31:0] x);
        logic [31:0] next;
        begin
            next = x ^ (x << 13);
            next = next ^ (next >> 17);
            advance_random = next ^ (next << 5);
        end
    endfunction

    logic [31:0] random_q = 32'h4973_14f6;

    // Independent reproducible stalls on both the decision and word boundaries.
    always @(negedge clk) begin
        random_q <= advance_random(random_q);
        sink_ready <= random_q[3:0] < 12;
        stream_ready <= random_q[8:5] < 11;
    end

    integer stimulus, expected, capture;
    integer cycle = 0, next_commit = 0;
    integer accepted = 0, committed = 0, replies_seen = 0, frames_seen = 0;
    integer cfg_blocked_cycles = 0, order_blocked_cycles = 0;
    integer response_stalls = 0, stream_stalls = 0;
    integer word_offset = 0;
    bit in_flight = 0;
    bit seen_reply_stall = 0, seen_word_stall = 0;
    logic [RECORD_W-1:0] held_record, expected_record;
    logic [33:0] held_word;
    logic [63:0] p_op, p_tag, p_acct, p_sym, p_side, p_slot, p_gen, p_ev;
    logic [63:0] p_code, p_live, p_qty, p_limit, p_res, p_open, p_exec, p_count;
    integer result_cols;

    // Compare one delivered decision with the next expected risk reply row.
    task automatic compare_next_reply;
        begin
            result_cols = $fscanf(expected,
                "%d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d",
                p_op, p_tag, p_acct, p_sym, p_side, p_slot, p_gen, p_ev,
                p_code, p_live, p_qty, p_limit, p_res, p_open, p_exec, p_count);
            if (result_cols != 16)
                $fatal(1, "expected 16 decision fields for reply %0d, got %0d", replies_seen, result_cols);
            expected_record = {
                3'(p_op), TAG_W'(p_tag), ACCOUNT_W'(p_acct), INSTRUMENT_W'(p_sym),
                1'(p_side), SLOT_W'(p_slot), GEN_W'(p_gen), EVENT_W'(p_ev),
                5'(p_code), 1'(p_live), QTY_W'(p_qty), PRICE_W'(p_limit),
                NOTIONAL_W'(p_res), NOTIONAL_W'(p_open), NOTIONAL_W'(p_exec), COUNT_W'(p_count)
            };
            if (record !== expected_record) begin
                $display("expected tag=%0d op=%0d code=%0d open=%0d exec=%0d count=%0d",
                         p_tag, p_op, p_code, p_open, p_exec, p_count);
                $display("observed tag=%0d op=%0d code=%0d open=%0d exec=%0d count=%0d",
                         m_tag, m_op, m_code, m_open_notional, m_executed_notional, m_open_orders);
                $fatal(1, "reply %0d: expected %h, got %h", replies_seen, expected_record, record);
            end
        end
    endtask

    // Fixed edge accounting is checked even when an older reply holds up egress.
    always @(posedge clk) begin
        cycle = cycle + 1;
        if (rst) begin
            if (s_ready !== 1'b0 || cfg_ready !== 1'b0 || m_valid !== 1'b0)
                $fatal(1, "handshake escaped while reset asserted");
            if (in_flight || seen_reply_stall)
                $fatal(1, "vector reset must use a drained boundary");
        end else begin
`ifndef RISK_POST_SYNTH
            if (dut.push) begin
                if (!in_flight || next_commit != cycle)
                    $fatal(1, "commit at %0d, expected %0d", cycle, next_commit);
                if (dut.reply_count >= 2 && !m_ready)
                    $fatal(1, "accepted operation did not reserve a reply slot");
                in_flight = 0;
                committed = committed + 1;
            end else if (in_flight && next_commit == cycle) begin
                $fatal(1, "missed deterministic commit at %0d", cycle);
            end
`endif
            if (s_valid && s_ready) begin
                if (in_flight || cfg_valid)
                    $fatal(1, "illegal issue overlap");
`ifndef RISK_POST_SYNTH
                next_commit = cycle + 3;
                in_flight = 1;
`endif
                accepted = accepted + 1;
            end
            if (cfg_valid && !cfg_ready)
                cfg_blocked_cycles = cfg_blocked_cycles + 1;
            if (s_valid && !s_ready)
                order_blocked_cycles = order_blocked_cycles + 1;
            if (seen_reply_stall && (!m_valid || record !== held_record))
                $fatal(1, "decision payload changed while stalled");
            if ((decision_valid && sink_ready) !== (m_valid && m_ready))
                $fatal(1, "audit tap changed the primary handshake");
            if (m_valid && m_ready) begin
                if (outgoing_record !== record)
                    $fatal(1, "audit tap changed the primary record");
                compare_next_reply();
                replies_seen = replies_seen + 1;
`ifdef RISK_POST_SYNTH
                committed = committed + 1;
`endif
            end
            if (m_valid && !m_ready)
                response_stalls = response_stalls + 1;
            seen_reply_stall = m_valid && !m_ready;
            held_record = record;
        end
    end

    always @(posedge clk) begin
        if (!audit_rst) begin
            if (seen_word_stall &&
                (!stream_valid || {stream_first, stream_last, stream_data} !== held_word))
                $fatal(1, "audit frame advanced without a transfer");
            if (stream_valid && stream_ready) begin
                if (stream_first !== (word_offset == 0) ||
                    stream_last !== (word_offset == FRAME_WORDS - 1))
                    $fatal(1, "incorrect frame edge flags at word %0d", word_offset);
                $fdisplay(capture, "%08x", stream_data);
                if (stream_last) begin
                    word_offset = 0;
                    frames_seen = frames_seen + 1;
                end else word_offset = word_offset + 1;
            end
            if (stream_valid && !stream_ready)
                stream_stalls = stream_stalls + 1;
            seen_word_stall = stream_valid && !stream_ready;
            held_word = {stream_first, stream_last, stream_data};
        end
    end

    logic [63:0] f0, f1, f2, f3, f4, f5, f6, f7, f8, f9;
    integer action, columns;
    string vector_dir;

    // Offer the next generated order request and wait for acceptance.
    task automatic push_order;
        begin
            @(negedge clk);
            s_op = 3'(f0);
            s_tag = TAG_W'(f1);
            s_account = ACCOUNT_W'(f2);
            s_instrument = INSTRUMENT_W'(f3);
            s_sell = 1'(f4);
            s_slot = SLOT_W'(f5);
            s_generation = GEN_W'(f6);
            s_event_seq = EVENT_W'(f7);
            s_price = PRICE_W'(f8);
            s_qty = QTY_W'(f9);
            s_valid = 1;
            do @(posedge clk); while (!s_ready);
            @(negedge clk);
            s_valid = 0;
        end
    endtask

    // Drive an indexed policy write while preserving ordering with the request stream.
    task automatic change_policy;
        begin
            @(negedge clk);
            cfg_account = ACCOUNT_W'(f0);
            cfg_instrument = INSTRUMENT_W'(f1);
            cfg_sell = 1'(f2);
            cfg_enable = 1'(f3);
            cfg_min_price = PRICE_W'(f4);
            cfg_max_price = PRICE_W'(f5);
            cfg_max_order_qty = QTY_W'(f6);
            cfg_max_open_notional = NOTIONAL_W'(f7);
            cfg_max_open_orders = COUNT_W'(f8);
            cfg_valid = 1;
            do @(posedge clk); while (!cfg_ready);
            @(negedge clk);
            cfg_valid = 0;
        end
    endtask

    // Wait for all outstanding generated responses and audit records to transfer.
    task automatic drain;
        begin
            do @(negedge clk); while (!cfg_ready || !audit_in_ready || audit_record_valid);
            if (in_flight || word_offset != 0 || replies_seen != frames_seen)
                $fatal(1, "drain reached inconsistent state");
        end
    endtask

    // Assert the emergency stop on a sampled edge before issuing later requests.
    task automatic halt_session;
        begin
            drain();
            @(negedge clk);
            halt_req = 1;
            @(negedge clk);
            halt_req = 0;
            @(posedge clk);
            if (!halted) $fatal(1, "halt request did not latch");
        end
    endtask

    // Assert reset and check that pending requests and outputs are discarded.
    task automatic reset_session;
        begin
            drain();
            @(negedge clk);
            rst = 1;
            @(negedge clk);
            rst = 0;
            @(posedge clk);
            if (halted) $fatal(1, "new session retained prior halt");
        end
    endtask

    initial begin
        if (!$value$plusargs("vector_dir=%s", vector_dir))
            $fatal(1, "usage: vvp SIM +vector_dir=build/PROFILE");
        stimulus = $fopen({vector_dir, "/stimulus.txt"}, "r");
        expected = $fopen({vector_dir, "/expected.txt"}, "r");
        capture = $fopen({vector_dir, "/audit.hex"}, "w");
        if (!stimulus || !expected || !capture)
            $fatal(1, "cannot read vectors or create trace in %s", vector_dir);

        rst = 1;
        audit_rst = 1;
        halt_req = 0;
        cfg_valid = 0;
        s_valid = 0;
        repeat (3) @(negedge clk);
        audit_rst = 0;
        rst = 0;

        columns = $fscanf(stimulus, "%d %d %d %d %d %d %d %d %d %d %d",
                          action, f0, f1, f2, f3, f4, f5, f6, f7, f8, f9);
        while (columns == 11) begin
            case (action)
                0: change_policy();
                1: push_order();
                2: halt_session();
                3: reset_session();
                4: drain();
                default: $fatal(1, "unknown vector action %0d", action);
            endcase
            columns = $fscanf(stimulus, "%d %d %d %d %d %d %d %d %d %d %d",
                              action, f0, f1, f2, f3, f4, f5, f6, f7, f8, f9);
        end
        if (columns > 0 || !$feof(stimulus))
            $fatal(1, "short stimulus row, got %0d fields", columns);
        drain();
        columns = $fscanf(expected, "%d", p_op);
        if (columns > 0 || !$feof(expected))
            $fatal(1, "expected stream still has unconsumed rows");
        if (accepted != committed || committed != replies_seen || replies_seen != frames_seen)
            $fatal(1, "counts differ: %0d accepted, %0d committed, %0d replied, %0d framed",
                   accepted, committed, replies_seen, frames_seen);
        if (dropped != 0 || drop_saturated)
            $fatal(1, "lossless audit tap dropped or saturated");
        if (response_stalls == 0 || stream_stalls == 0 || cfg_blocked_cycles == 0 ||
            order_blocked_cycles == 0)
            $fatal(1, "generated traffic missed a backpressure boundary");
        $fclose(stimulus);
        $fclose(expected);
        $fclose(capture);
        $display("PASS risk differential %0dx%0d: %0d commits and frames, %0d request / %0d response / %0d audit stalls",
                 PRICE_W, QTY_W, replies_seen, order_blocked_cycles, response_stalls, stream_stalls);
        $finish;
    end

    initial begin
        #40000000;
        $fatal(1, "differential test watchdog");
    end
endmodule

`default_nettype wire
