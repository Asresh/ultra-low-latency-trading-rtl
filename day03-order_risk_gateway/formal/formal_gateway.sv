`default_nettype none

module formal_gateway;
    localparam integer PRICE_W = 3;
    localparam integer QTY_W = 3;
    localparam integer TAG_W = 3;
    localparam integer GEN_W = 2;
    localparam integer EVENT_W = 2;
    localparam integer NOTIONAL_W = PRICE_W + QTY_W;

    (* gclk *) logic clk;
    (* anyseq *) logic rst, halt_req;
    (* anyseq *) logic cfg_valid, cfg_enable, cfg_sell;
    (* anyseq *) logic [PRICE_W-1:0] cfg_min_price, cfg_max_price;
    (* anyseq *) logic [QTY_W-1:0] cfg_max_order_qty;
    (* anyseq *) logic [NOTIONAL_W-1:0] cfg_max_open_notional;

    (* anyseq *) logic s_valid, s_sell, m_ready;
    (* anyseq *) logic [2:0] s_op;
    (* anyseq *) logic [TAG_W-1:0] s_tag;
    (* anyseq *) logic [GEN_W-1:0] s_generation;
    (* anyseq *) logic [EVENT_W-1:0] s_event_seq;
    (* anyseq *) logic [PRICE_W-1:0] s_price;
    (* anyseq *) logic [QTY_W-1:0] s_qty;

    wire halted, cfg_ready, s_ready, m_valid;
    wire [2:0] m_op;
    wire [TAG_W-1:0] m_tag;
    wire m_sell, m_live;
    wire [GEN_W-1:0] m_generation;
    wire [EVENT_W-1:0] m_event_seq;
    wire [4:0] m_code;
    wire [QTY_W-1:0] m_leaves;
    wire [PRICE_W-1:0] m_limit_price;
    wire [NOTIONAL_W-1:0] m_reserved, m_open_notional, m_executed_notional;
    wire m_open_orders;

    hft_order_risk_gateway #(
        .NUM_ACCOUNTS(1), .NUM_INSTRUMENTS(1), .ORDER_SLOTS(1), .PRICE_W(PRICE_W),
        .QTY_W(QTY_W), .TAG_W(TAG_W), .GEN_W(GEN_W), .EVENT_W(EVENT_W)
    ) dut (
        .clk(clk), .rst(rst), .halt_req(halt_req), .halted(halted),
        .cfg_valid(cfg_valid), .cfg_ready(cfg_ready), .cfg_account(1'b0),
        .cfg_instrument(1'b0), .cfg_sell(cfg_sell), .cfg_enable(cfg_enable),
        .cfg_min_price(cfg_min_price), .cfg_max_price(cfg_max_price),
        .cfg_max_order_qty(cfg_max_order_qty), .cfg_max_open_notional(cfg_max_open_notional),
        .cfg_max_open_orders(1'b1),
        .s_valid(s_valid), .s_ready(s_ready), .s_op(s_op), .s_tag(s_tag),
        .s_account(1'b0), .s_instrument(1'b0), .s_sell(s_sell), .s_slot(1'b0),
        .s_generation(s_generation), .s_event_seq(s_event_seq), .s_price(s_price), .s_qty(s_qty),
        .m_valid(m_valid), .m_ready(m_ready), .m_op(m_op), .m_tag(m_tag), .m_account(),
        .m_instrument(), .m_sell(m_sell), .m_slot(), .m_generation(m_generation),
        .m_event_seq(m_event_seq), .m_code(m_code), .m_slot_live(m_live), .m_leaves(m_leaves),
        .m_limit_price(m_limit_price), .m_reserved(m_reserved), .m_open_notional(m_open_notional),
        .m_executed_notional(m_executed_notional), .m_open_orders(m_open_orders)
    );

    localparam integer REQ_BITS = 3 + TAG_W + 1;
    wire [REQ_BITS-1:0] identity_in = {s_op, s_tag, s_sell};
    wire [REQ_BITS-1:0] identity_out = {m_op, m_tag, m_sell};
    wire [REQ_BITS + 5 + 2 + GEN_W + EVENT_W + PRICE_W + QTY_W + 3*NOTIONAL_W-1:0] result = {
        identity_out, m_code, m_live, m_open_orders, m_generation, m_event_seq,
        m_limit_price, m_leaves, m_reserved, m_open_notional, m_executed_notional
    };
    wire [NOTIONAL_W-1:0] release_value = m_limit_price * m_leaves;
    wire enqueue = s_ready && s_valid;
    wire dequeue = m_ready && m_valid;

    logic past_valid = 1'b0;
    logic [1:0] occupancy;
    logic [3*REQ_BITS-1:0] identities, next_identities;
    logic seen_admission, seen_held_reply;

    always_comb begin
        next_identities = dequeue ? (identities >> REQ_BITS) : identities;
        if (enqueue)
            next_identities[(int'(occupancy) - int'(dequeue))*REQ_BITS+:REQ_BITS] = identity_in;
    end

    always @(posedge clk) begin
        if (!past_valid)
            assume(rst);
        past_valid <= 1'b1;

        if (rst) begin
            occupancy <= '0;
            identities <= '0;
            seen_admission <= 1'b0;
            seen_held_reply <= 1'b0;
            assert(!s_ready && !cfg_ready && !m_valid);
        end else begin
            // Independent accounting of accepted identities tests ordered retirement.
            assert(!(cfg_valid && cfg_ready && enqueue));
            if (enqueue && !dequeue) assert(occupancy < 3);
            if (m_valid) begin
                assert(occupancy != 0);
                assert(identity_out == identities[REQ_BITS-1:0]);
                if (m_live) begin
                    assert(m_generation != 0);
                    assert(m_leaves != 0 && m_limit_price != 0);
                    assert(m_reserved == release_value);
                    assert(m_reserved == m_open_notional && m_open_orders);
                end else begin
                    assert(m_leaves == 0 && m_reserved == 0);
                end
                if (!m_open_orders) assert(m_open_notional == 0);
            end
            if (past_valid && $past(!rst && m_valid && !m_ready)) begin
                assert(m_valid);
                assert(result == $past(result));
            end
            if (past_valid && $past(!rst && halted)) assert(halted);
            if (halt_req) assert(halted);

            identities <= next_identities;
            case ({enqueue, dequeue})
                2'b10: occupancy <= occupancy + 1'b1;
                2'b01: occupancy <= occupancy - 1'b1;
                default: occupancy <= occupancy;
            endcase
            if (m_valid && m_code == 0 && m_op == 0) seen_admission <= 1'b1;
            if (m_valid && !m_ready) seen_held_reply <= 1'b1;

            cover(seen_admission && m_valid && m_code == 0 && m_op == 1 &&
                  !m_live && m_executed_notional != 0);
            cover(seen_admission && m_valid && m_code == 9);
            cover(seen_held_reply && m_valid && m_ready);
            cover(seen_admission && halted && m_valid && m_code == 0 && m_op == 2);
        end
    end
endmodule

`default_nettype wire
