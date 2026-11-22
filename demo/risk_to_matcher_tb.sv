`timescale 1ns/1ps

module risk_to_matcher_tb;
    logic clk = 0;
    always #5 clk = !clk;

    logic rst = 1, halt_req = 0;
    logic cfg_valid = 0;
    wire cfg_ready;
    logic order_valid = 0, order_sell;
    wire order_ready;
    logic [4:0] order_tag, order_price;
    logic [15:0] order_qty;

    wire risk_valid, risk_ready, risk_allow, risk_sell;
    wire [4:0] risk_tag, risk_price;
    wire [7:0] risk_reject;
    wire [15:0] risk_qty;
    wire [20:0] risk_gross;

    wire book_ready, event_valid, event_last;
    logic event_ready = 0;
    wire [1:0] event_kind;
    wire [4:0] event_id, maker_id, event_price, bid_price, ask_price;
    wire [15:0] event_qty, event_remaining;
    wire bid_valid, ask_valid;

    pretrade_risk_gateway #(.NUM_INSTRUMENTS(1), .PRICE_W(5), .QTY_W(16), .TAG_W(5)) gateway (
        .clk(clk), .rst(rst), .halt_req(halt_req), .halted(),
        .cfg_valid(cfg_valid), .cfg_ready(cfg_ready), .cfg_instrument(1'b0), .cfg_enable(1'b1),
        .cfg_min_price(5'd1), .cfg_max_price(5'd15), .cfg_max_qty(16'd10),
        .cfg_max_order_notional(21'd100), .cfg_max_buy_gross(21'd100), .cfg_max_sell_gross(21'd100),
        .s_valid(order_valid), .s_ready(order_ready), .s_tag(order_tag),
        .s_instrument(1'b0), .s_sell(order_sell), .s_price(order_price), .s_qty(order_qty),
        .m_valid(risk_valid), .m_ready(risk_ready), .m_tag(risk_tag), .m_instrument(),
        .m_sell(risk_sell), .m_price(risk_price), .m_qty(risk_qty), .m_allow(risk_allow),
        .m_reject(risk_reject), .m_notional(), .m_gross_after(risk_gross)
    );

    // The decision sink records every outcome. Only allows enter the matcher.
    assign risk_ready = !risk_allow || book_ready;
    price_time_matcher book (
        .clk(clk), .rst(rst), .s_valid(risk_valid && risk_allow), .s_ready(book_ready),
        .s_cancel(1'b0), .s_is_ask(risk_sell), .s_order_id(risk_tag),
        .s_price(risk_price), .s_qty(risk_qty),
        .m_valid(event_valid), .m_ready(event_ready), .m_kind(event_kind), .m_last(event_last),
        .m_order_id(event_id), .m_is_ask(), .m_maker_id(maker_id), .m_price(event_price),
        .m_qty(event_qty), .m_remaining(event_remaining), .m_reject(),
        .bid_valid(bid_valid), .bid_price(bid_price), .ask_valid(ask_valid), .ask_price(ask_price)
    );

    int decisions = 0, events = 0, stalls = 0;

    task automatic submit(input logic [4:0] tag, input logic sell,
                          input logic [4:0] price, input logic [15:0] qty);
        @(negedge clk);
        order_tag = tag;
        order_sell = sell;
        order_price = price;
        order_qty = qty;
        order_valid = 1;
        do @(posedge clk); while (!order_ready);
        @(negedge clk);
        order_valid = 0;
    endtask

    task automatic expect_decision(input logic [7:0] reject, input logic [20:0] gross);
        if ({risk_allow, risk_reject, risk_gross} !== {reject == 0, reject, gross})
            $fatal(1, "Unexpected risk decision for tag %0d: reject=%h gross=%0d",
                   risk_tag, risk_reject, risk_gross);
    endtask

    task automatic expect_event(
        input logic [1:0] kind,
        input logic [4:0] id, maker, price,
        input logic [15:0] qty, remaining,
        input logic last
    );
        if ({event_kind, event_id, maker_id, event_price, event_qty, event_remaining, event_last}
            !== {kind, id, maker, price, qty, remaining, last})
            $fatal(1, "Unexpected event %0d: kind=%0d id=%0d maker=%0d price=%0d qty=%0d rem=%0d",
                   events, event_kind, event_id, maker_id, event_price, event_qty, event_remaining);
    endtask

    always @(posedge clk) begin
        if (!rst && risk_valid && !risk_ready) stalls = stalls + 1;
        if (!rst && risk_valid && risk_ready) begin
            if (risk_tag !== 5'(decisions)) $fatal(1, "Decision order changed or duplicated");
            case (decisions)
                0: expect_decision(8'h00, 21'd30);
                1: expect_decision(8'h00, 21'd80);
                2: expect_decision(8'h04, 21'd80);
                3: expect_decision(8'h00, 21'd66);
                4: expect_decision(8'h40, 21'd66);
                5: expect_decision(8'h00, 21'd84);
                6: expect_decision(8'h80, 21'd80);
                default: $fatal(1, "Extra decision");
            endcase
            decisions = decisions + 1;
        end
        if (!rst && event_valid && event_ready) begin
            case (events)
                0: expect_event(2'd1, 5'd0, 5'd0, 5'd10, 16'd3, 16'd3, 1'b1);
                1: expect_event(2'd1, 5'd1, 5'd0, 5'd10, 16'd5, 16'd5, 1'b1);
                2: expect_event(2'd0, 5'd3, 5'd0, 5'd10, 16'd3, 16'd3, 1'b0);
                3: expect_event(2'd0, 5'd3, 5'd1, 5'd10, 16'd3, 16'd0, 1'b1);
                4: expect_event(2'd1, 5'd5, 5'd0, 5'd9, 16'd2, 16'd2, 1'b1);
                default: $fatal(1, "Extra event from matcher");
            endcase
            events = events + 1;
        end
    end

    initial begin
        repeat (3) @(negedge clk);
        rst = 0;
        cfg_valid = 1;
        do @(posedge clk); while (!cfg_ready);
        @(negedge clk);
        cfg_valid = 0;
        fork
            begin
                repeat (18) @(negedge clk);
                event_ready = 1;
            end
            begin
                submit(5'd0, 1'b1, 5'd10, 16'd3);
                submit(5'd1, 1'b1, 5'd10, 16'd5);
                submit(5'd2, 1'b1, 5'd9, 16'd0);
                submit(5'd3, 1'b0, 5'd11, 16'd6);
                submit(5'd4, 1'b0, 5'd11, 16'd5);
                submit(5'd5, 1'b0, 5'd9, 16'd2);
            end
        join
        wait (decisions == 6 && events == 5);
        @(negedge clk);
        if ({bid_valid, bid_price, ask_valid, ask_price} !== {1'b1, 5'd9, 1'b1, 5'd10})
            $fatal(1, "Unexpected final top of book");
        halt_req = 1;
        @(negedge clk);
        halt_req = 0;
        submit(5'd6, 1'b1, 5'd10, 16'd1);
        wait (decisions == 7);
        repeat (5) @(negedge clk);
        if (stalls == 0 || events != 5) $fatal(1, "Backpressure or isolation was not exercised");
        $display("PASS integration: %0d risk decisions, %0d book events, %0d risk stall cycles",
                 decisions, events, stalls);
        $finish;
    end

    initial begin
        #5000;
        $fatal(1, "Integration timeout");
    end
endmodule
