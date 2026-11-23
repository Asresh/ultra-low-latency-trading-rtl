`timescale 1ns/1ps

module tb_risk_match_lifecycle;
    localparam int BANKS = 2;
    localparam int SLOTS = 6;
    localparam int PRICE_LEVELS = 12;
    localparam int PRICE_W = $clog2(PRICE_LEVELS + 1);
    localparam int SLOT_W = $clog2(SLOTS);
    localparam int BOOK_ID_W = $clog2(SLOTS + 1);
    localparam int BANK_W = $clog2(BANKS + 1);
    localparam int QTY_W = 8;
    localparam int TAG_W = 8;
    localparam int GEN_W = 4;
    localparam int EVENT_W = 6;
    localparam int NOTIONAL_W = PRICE_W + QTY_W;
    localparam int COUNT_W = $clog2(SLOTS + 1);

    localparam logic [2:0] RISK_NEW = 0, RISK_FILL = 1, RISK_CANCEL = 2;
    localparam logic [4:0] OK = 0, STALE_GENERATION = 9, BAD_SEQUENCE = 10, QTY_LIMIT = 12;
    localparam logic [1:0] BOOK_NEW = 0, BOOK_CANCEL = 1;
    localparam logic [2:0] BOOK_FILL = 0, BOOK_REST = 1, BOOK_CANCELED = 2;

    localparam logic [SLOT_W-1:0] MAKER = 2;
    localparam logic [SLOT_W-1:0] TAKER = 3;

    typedef struct packed {
        logic [2:0] op;
        logic [TAG_W-1:0] tag;
        logic account;
        logic instrument;
        logic sell;
        logic [SLOT_W-1:0] slot;
        logic [GEN_W-1:0] generation;
        logic [EVENT_W-1:0] seq;
        logic [PRICE_W-1:0] price;
        logic [QTY_W-1:0] qty;
    } risk_request_t;

    typedef struct packed {
        logic [2:0] op;
        logic [TAG_W-1:0] tag;
        logic account;
        logic instrument;
        logic sell;
        logic [SLOT_W-1:0] slot;
        logic [GEN_W-1:0] generation;
        logic [EVENT_W-1:0] seq;
        logic [4:0] code;
        logic live;
        logic [QTY_W-1:0] leaves;
        logic [PRICE_W-1:0] limit_price;
        logic [NOTIONAL_W-1:0] reserved;
        logic [NOTIONAL_W-1:0] open_amount;
        logic [NOTIONAL_W-1:0] executed_amount;
        logic [COUNT_W-1:0] open_count;
    } risk_reply_t;

    typedef struct packed {
        logic [BANK_W-1:0] instrument;
        logic [2:0] kind;
        logic last;
        logic [BOOK_ID_W-1:0] id;
        logic sell;
        logic [BOOK_ID_W-1:0] maker;
        logic [PRICE_W-1:0] price;
        logic [QTY_W-1:0] qty;
        logic [QTY_W-1:0] remaining;
        logic [3:0] reject;
        logic [TAG_W-1:0] tag;
    } book_reply_t;

    logic clk = 0;
    always #5 clk = !clk;
    logic rst = 1;
    logic cfg_valid = 0, cfg_account = 0, cfg_sell = 0;
    wire cfg_ready;
    logic risk_in_valid = 0, risk_out_ready = 0;
    wire risk_in_ready, risk_out_valid;
    risk_request_t request;
    wire risk_reply_t risk_out;

    logic book_in_valid = 0, book_out_ready = 0, book_sell;
    logic [1:0] book_op;
    logic [BOOK_ID_W-1:0] book_id;
    logic [PRICE_W-1:0] book_price;
    logic [QTY_W-1:0] book_qty;
    logic [TAG_W-1:0] book_tag;
    wire book_in_ready, book_out_valid;
    wire book_reply_t book_out;

    hft_order_risk_gateway #(
        .NUM_ACCOUNTS(2), .NUM_INSTRUMENTS(BANKS), .ORDER_SLOTS(SLOTS),
        .TAG_W(TAG_W), .PRICE_W(PRICE_W), .QTY_W(QTY_W), .GEN_W(GEN_W), .EVENT_W(EVENT_W)
    ) risk (
        .clk(clk), .rst(rst), .halt_req(1'b0), .halted(),
        .cfg_valid(cfg_valid), .cfg_ready(cfg_ready), .cfg_account(cfg_account),
        .cfg_instrument(1'b0), .cfg_sell(cfg_sell), .cfg_enable(1'b1),
        .cfg_min_price(PRICE_W'(1)), .cfg_max_price(PRICE_W'(11)),
        .cfg_max_order_qty(QTY_W'(16)), .cfg_max_open_notional(NOTIONAL_W'(120)),
        .cfg_max_open_orders(COUNT_W'(2)),
        .s_valid(risk_in_valid), .s_ready(risk_in_ready), .s_op(request.op), .s_tag(request.tag),
        .s_account(request.account), .s_instrument(request.instrument), .s_sell(request.sell),
        .s_slot(request.slot), .s_generation(request.generation), .s_event_seq(request.seq),
        .s_price(request.price), .s_qty(request.qty),
        .m_valid(risk_out_valid), .m_ready(risk_out_ready), .m_op(risk_out.op), .m_tag(risk_out.tag),
        .m_account(risk_out.account), .m_instrument(risk_out.instrument), .m_sell(risk_out.sell),
        .m_slot(risk_out.slot), .m_generation(risk_out.generation), .m_event_seq(risk_out.seq),
        .m_code(risk_out.code), .m_slot_live(risk_out.live), .m_leaves(risk_out.leaves),
        .m_limit_price(risk_out.limit_price), .m_reserved(risk_out.reserved),
        .m_open_notional(risk_out.open_amount), .m_executed_notional(risk_out.executed_amount),
        .m_open_orders(risk_out.open_count)
    );

    banked_matching_engine #(
        .BANKS(BANKS), .PRICE_LEVELS(PRICE_LEVELS), .ORDER_SLOTS(SLOTS),
        .QTY_W(QTY_W), .TAG_W(TAG_W), .SEQ_W(8), .CMD_DEPTH(2), .EVENT_DEPTH(2)
    ) matching (
        .clk(clk), .rst(rst), .s_valid(book_in_valid), .s_ready(book_in_ready),
        .s_instrument(BANK_W'(0)), .s_opcode(book_op), .s_tif(2'd0),
        .s_is_ask(book_sell), .s_order_id(book_id), .s_price(book_price),
        .s_qty(book_qty), .s_tag(book_tag),
        .m_valid(book_out_valid), .m_ready(book_out_ready), .m_instrument(book_out.instrument),
        .m_kind(book_out.kind), .m_last(book_out.last), .m_order_id(book_out.id),
        .m_is_ask(book_out.sell), .m_maker_id(book_out.maker), .m_price(book_out.price),
        .m_qty(book_out.qty), .m_remaining(book_out.remaining), .m_reject(book_out.reject),
        .m_level_qty(), .m_level_count(), .m_tag(book_out.tag), .m_cmd_seq(), .m_event_seq()
    );

    int risk_replies = 0;
    int book_replies = 0;

    task automatic configure(input logic account, sell);
        @(negedge clk);
        cfg_account = account;
        cfg_sell = sell;
        cfg_valid = 1;
        do @(posedge clk); while (!cfg_ready);
        @(negedge clk);
        cfg_valid = 0;
    endtask

    task automatic apply_risk(input risk_request_t item, output risk_reply_t reply);
        @(negedge clk);
        request = item;
        risk_in_valid = 1;
        do @(posedge clk); while (!risk_in_ready);
        @(negedge clk);
        risk_in_valid = 0;
        do @(negedge clk); while (!risk_out_valid);
        reply = risk_out;
        repeat (3) begin
            @(negedge clk);
            if (!risk_out_valid || risk_out !== reply) $fatal(1, "Risk reply changed while held");
        end
        risk_out_ready = 1;
        @(posedge clk);
        risk_replies++;
        @(negedge clk);
        risk_out_ready = 0;
        if ({reply.op, reply.tag, reply.account, reply.instrument, reply.sell, reply.slot} !==
            {item.op, item.tag, item.account, item.instrument, item.sell, item.slot})
            $fatal(1, "Risk response correlation failure");
    endtask

    task automatic apply_book(
        input logic [1:0] op,
        input risk_request_t approved,
        output book_reply_t reply
    );
        @(negedge clk);
        book_op = op;
        book_sell = approved.sell;
        book_id = BOOK_ID_W'(approved.slot);
        book_price = approved.price;
        book_qty = approved.qty;
        book_tag = approved.tag;
        book_in_valid = 1;
        do @(posedge clk); while (!book_in_ready);
        @(negedge clk);
        book_in_valid = 0;
        do @(negedge clk); while (!book_out_valid);
        reply = book_out;
        repeat (2) begin
            @(negedge clk);
            if (!book_out_valid || book_out !== reply) $fatal(1, "Book event changed while held");
        end
        book_out_ready = 1;
        @(posedge clk);
        book_replies++;
        @(negedge clk);
        book_out_ready = 0;
        if (reply.tag !== approved.tag || reply.id !== BOOK_ID_W'(approved.slot) ||
            reply.instrument !== '0 || reply.reject !== '0 || !reply.last)
            $fatal(1, "Book response correlation failure");
    endtask

    task automatic expect_ledger(
        input risk_reply_t reply,
        input logic [4:0] code,
        input logic live,
        input logic [QTY_W-1:0] leaves,
        input logic [NOTIONAL_W-1:0] reserved, open_amount, executed,
        input logic [COUNT_W-1:0] count
    );
        if ({reply.code, reply.live, reply.leaves, reply.reserved,
             reply.open_amount, reply.executed_amount, reply.open_count} !==
            {code, live, leaves, reserved, open_amount, executed, count})
            $fatal(1, "Ledger mismatch for tag=%0d code=%0d qty=%0d reserve=%0d open=%0d exec=%0d",
                   reply.tag, reply.code, reply.leaves, reply.reserved,
                   reply.open_amount, reply.executed_amount);
    endtask

    risk_request_t order, maker_fill;
    risk_reply_t risk_reply;
    book_reply_t book_reply, trade;
    logic [GEN_W-1:0] maker_generation, taker_generation;
    initial begin
        repeat (3) @(negedge clk);
        rst = 0;
        configure(1'b0, 1'b1);
        configure(1'b1, 1'b0);

        // An order beyond the configured size cap never crosses the venue boundary.
        order = {RISK_NEW, TAG_W'(1), 1'b0, 1'b0, 1'b1, MAKER,
                 GEN_W'(0), EVENT_W'(0), PRICE_W'(9), QTY_W'(17)};
        apply_risk(order, risk_reply);
        expect_ledger(risk_reply, QTY_LIMIT, 1'b0, 0, 0, 0, 0, 0);
        repeat (4) @(negedge clk);
        if (book_out_valid) $fatal(1, "A risk reject produced a venue event");

        // Sell seven at tick 9. Reserve 63 before allowing the venue order.
        order.tag = 2;
        order.qty = 7;
        apply_risk(order, risk_reply);
        expect_ledger(risk_reply, OK, 1'b1, 7, 63, 63, 0, 1);
        maker_generation = risk_reply.generation;
        apply_book(BOOK_NEW, order, book_reply);
        if (book_reply.kind != BOOK_REST || book_reply.qty != 7 || book_reply.price != 9)
            $fatal(1, "Maker did not rest");

        // Buy four at tick 10. The book trades at the maker's tick, not the bid limit.
        order = {RISK_NEW, TAG_W'(3), 1'b1, 1'b0, 1'b0, TAKER,
                 GEN_W'(0), EVENT_W'(0), PRICE_W'(10), QTY_W'(4)};
        apply_risk(order, risk_reply);
        expect_ledger(risk_reply, OK, 1'b1, 4, 40, 40, 0, 1);
        taker_generation = risk_reply.generation;
        apply_book(BOOK_NEW, order, trade);
        if (trade.kind != BOOK_FILL || trade.maker != BOOK_ID_W'(MAKER) ||
            trade.price != 9 || trade.qty != 4)
            $fatal(1, "Expected a four-share fill at the resting price");

        // Convert one exchange fill into separate reports for the maker and taker.
        maker_fill = {RISK_FILL, TAG_W'(4), 1'b0, 1'b0, 1'b1, MAKER,
                      maker_generation, EVENT_W'(1), trade.price, trade.qty};
        apply_risk(maker_fill, risk_reply);
        expect_ledger(risk_reply, OK, 1'b1, 3, 27, 27, 36, 1);
        order = {RISK_FILL, TAG_W'(5), 1'b1, 1'b0, 1'b0, TAKER,
                 taker_generation, EVENT_W'(1), trade.price, trade.qty};
        apply_risk(order, risk_reply);
        expect_ledger(risk_reply, OK, 1'b0, 0, 0, 0, 36, 0);
        maker_fill.tag = 6;
        apply_risk(maker_fill, risk_reply);
        expect_ledger(risk_reply, BAD_SEQUENCE, 1'b1, 3, 27, 27, 36, 1);

        // Confirm the venue cancellation before releasing the open reservation.
        order = {RISK_CANCEL, TAG_W'(7), 1'b0, 1'b0, 1'b1, MAKER,
                 maker_generation, EVENT_W'(2), PRICE_W'(0), QTY_W'(0)};
        apply_book(BOOK_CANCEL, order, book_reply);
        if (book_reply.kind != BOOK_CANCELED || book_reply.qty != 3)
            $fatal(1, "Cancel did not remove the three-share remainder");
        apply_risk(order, risk_reply);
        expect_ledger(risk_reply, OK, 1'b0, 0, 0, 0, 36, 0);

        order = {RISK_NEW, TAG_W'(8), 1'b0, 1'b0, 1'b1, MAKER,
                 GEN_W'(0), EVENT_W'(0), PRICE_W'(8), QTY_W'(2)};
        apply_risk(order, risk_reply);
        expect_ledger(risk_reply, OK, 1'b1, 2, 16, 16, 36, 1);
        if (risk_reply.generation !== maker_generation + GEN_W'(1)) $fatal(1, "Slot alias");
        apply_book(BOOK_NEW, order, book_reply);
        if (book_reply.kind != BOOK_REST || book_reply.qty != 2 || book_reply.price != 8)
            $fatal(1, "Replacement did not rest");

        maker_fill.tag = 9;
        apply_risk(maker_fill, risk_reply);
        expect_ledger(risk_reply, STALE_GENERATION, 1'b1, 2, 16, 16, 36, 1);

        if (risk_replies != 9 || book_replies != 4) $fatal(1, "Incomplete lifecycle");
        $display("PASS risk/matcher lifecycle: %0d risk replies, %0d book events", risk_replies, book_replies);
        $finish;
    end

    initial begin
        #20000;
        $fatal(1, "risk/matcher lifecycle timeout");
    end
endmodule
