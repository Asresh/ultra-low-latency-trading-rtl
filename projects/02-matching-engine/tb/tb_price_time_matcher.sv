`timescale 1ns/1ps
`default_nettype none

module tb_price_time_matcher #(
    parameter integer PRICE_LEVELS = 7,
    parameter integer ORDER_SLOTS = 5,
    parameter integer QW = 8,
    parameter integer RANDOM_COMMANDS = 2500
);
    localparam integer PW = $clog2(PRICE_LEVELS + 1);
    localparam integer IW = $clog2(ORDER_SLOTS + 1);
    localparam integer MAX_QTY = (1 << QW) - 1;
    localparam integer FILL = 0, REST = 1, CANCEL = 2, REJECT = 3;

    logic clk = 1'b0;
    always #5 clk = !clk;
    logic rst, s_valid, s_cancel, s_is_ask, m_ready;
    logic [IW-1:0] s_order_id;
    logic [PW-1:0] s_price;
    logic [QW-1:0] s_qty;
    wire s_ready, m_valid, m_last, m_is_ask, bid_valid, ask_valid;
    wire [1:0] m_kind;
    wire [IW-1:0] m_order_id, m_maker_id;
    wire [PW-1:0] m_price, bid_price, ask_price;
    wire [QW-1:0] m_qty, m_remaining;
    wire [2:0] m_reject;

    price_time_matcher #(
        .PRICE_LEVELS(PRICE_LEVELS), .ORDER_SLOTS(ORDER_SLOTS), .QTY_W(QW)
    ) dut (.*);

    // Reference uses a linear scan and insertion timestamp. It has no
    // occupancy bitmap, priority tree, or linked-list maintenance.
    bit in_book [0:ORDER_SLOTS-1];
    bit side [0:ORDER_SLOTS-1];
    integer price [0:ORDER_SLOTS-1];
    integer qty [0:ORDER_SLOTS-1];
    integer stamp [0:ORDER_SLOTS-1];
    integer next_stamp = 0;
    integer commands = 0, events = 0, stalls = 0, fills = 0;
    integer partial_makers = 0, continued_takers = 0;
    integer rests = 0, cancellations = 0, rejects = 0;
    logic [31:0] rng = 32'h7ea5_1b10;

    typedef struct packed {
        logic [1:0] kind;
        logic last;
        logic [IW-1:0] id;
        logic is_ask;
        logic [IW-1:0] maker;
        logic [PW-1:0] px;
        logic [QW-1:0] volume;
        logic [QW-1:0] left;
        logic [2:0] reject;
    } event_t;

    function automatic integer draw(input integer span);
        rng = rng ^ (rng << 13);
        rng = rng ^ (rng >> 17);
        rng = rng ^ (rng << 5);
        draw = int'(rng % $unsigned(span));
    endfunction

    function automatic integer best(input bit ask);
        integer winner;
        winner = -1;
        for (integer i = 0; i < ORDER_SLOTS; i++) begin
            if (in_book[i] && side[i] == ask) begin
                if (winner == -1)
                    winner = i;
                else if ((ask && price[i] < price[winner]) ||
                         (!ask && price[i] > price[winner]) ||
                         (price[i] == price[winner] && stamp[i] < stamp[winner]))
                    winner = i;
            end
        end
        best = winner;
    endfunction

    function automatic event_t predict(
        input bit cancel, input bit ask, input integer id,
        input integer px, input integer remaining
    );
        event_t event_out;
        integer maker, matched;
        event_out = '0;
        event_out.kind = REJECT;
        event_out.last = 1'b1;
        event_out.id = IW'(id);
        maker = best(!ask);
        if (id >= ORDER_SLOTS)
            event_out.reject = 1;
        else if (cancel) begin
            if (in_book[id]) begin
                event_out.kind = CANCEL;
                event_out.is_ask = side[id];
                event_out.px = PW'(price[id]);
                event_out.volume = QW'(qty[id]);
            end else
                event_out.reject = 5;
        end else if (px >= PRICE_LEVELS)
            event_out.reject = 2;
        else if (remaining == 0)
            event_out.reject = 3;
        else if (in_book[id])
            event_out.reject = 4;
        else if (maker >= 0 && (ask ? price[maker] >= px : price[maker] <= px)) begin
            matched = (remaining < qty[maker]) ? remaining : qty[maker];
            event_out.kind = FILL;
            event_out.last = remaining == matched;
            event_out.is_ask = ask;
            event_out.maker = IW'(maker);
            event_out.px = PW'(price[maker]);
            event_out.volume = QW'(matched);
            event_out.left = QW'(remaining - matched);
        end else begin
            event_out.kind = REST;
            event_out.is_ask = ask;
            event_out.px = PW'(px);
            event_out.volume = QW'(remaining);
            event_out.left = QW'(remaining);
        end
        predict = event_out;
    endfunction

    task automatic check_event(input event_t expected);
        event_t actual;
        actual = {m_kind, m_last, m_order_id, m_is_ask, m_maker_id, m_price,
                  m_qty, m_remaining, m_reject};
        if (m_valid !== 1'b1 || s_ready !== 1'b0 || actual !== expected)
            $fatal(1, "command %0d event %0d: valid=%b ready=%b expected=%h actual=%h",
                   commands, events, m_valid, s_ready, expected, actual);
    endtask

    task automatic check_book;
        integer bb, ba, ptr, prev, prior_stamp;
        bit [ORDER_SLOTS-1:0] visited;
        bb = best(1'b0);
        ba = best(1'b1);
        if (bid_valid !== (bb >= 0) || bid_price !== ((bb >= 0) ? PW'(price[bb]) : PW'(0)) ||
            ask_valid !== (ba >= 0) || ask_price !== ((ba >= 0) ? PW'(price[ba]) : PW'(0)))
            $fatal(1, "command %0d event %0d: top of book mismatch", commands, events);
        if (bb >= 0 && ba >= 0 && price[bb] >= price[ba])
            $fatal(1, "reference book crossed");

        visited = '0;
        for (integer a = 0; a < 2; a++) begin
            for (integer p = 0; p < PRICE_LEVELS; p++) begin
                if ((a == 0 && dut.bid_occupied[p]) || (a == 1 && dut.ask_occupied[p])) begin
                    ptr = int'(dut.head_id[a][p]);
                    prev = ORDER_SLOTS;
                    prior_stamp = -1;
                    if (ptr == ORDER_SLOTS)
                        $fatal(1, "occupied level %0d/%0d has an empty head", a, p);
                    while (ptr != ORDER_SLOTS) begin
                        if ($isunknown(ptr) || ptr < 0 || ptr >= ORDER_SLOTS)
                            $fatal(1, "price queue has an invalid pointer at level %0d/%0d", a, p);
                        if (visited[ptr] || !in_book[ptr] || side[ptr] != (a == 1) ||
                            price[ptr] != p || stamp[ptr] <= prior_stamp ||
                            dut.previous_id[ptr] !== IW'(prev))
                            $fatal(1, "invalid queue membership or priority at slot %0d", ptr);
                        visited[ptr] = 1'b1;
                        prior_stamp = stamp[ptr];
                        prev = ptr;
                        ptr = int'(dut.next_id[ptr]);
                    end
                    if (dut.tail_id[a][p] !== IW'(prev))
                        $fatal(1, "bad tail at level %0d/%0d", a, p);
                end
            end
        end
        for (integer i = 0; i < ORDER_SLOTS; i++) begin
            if (dut.live[i] !== in_book[i] || visited[i] != in_book[i])
                $fatal(1, "slot %0d not reachable exactly once when live", i);
            if (in_book[i] && (dut.order_price[i] !== PW'(price[i]) ||
                dut.order_qty[i] !== QW'(qty[i]) || dut.order_is_ask[i] !== side[i]))
                $fatal(1, "live slot %0d metadata mismatch", i);
        end
    endtask

    task automatic update_model(input event_t accepted);
        integer id, maker;
        id = int'(accepted.id);
        maker = int'(accepted.maker);
        events++;
        case (accepted.kind)
            FILL: begin
                fills++;
                if (!accepted.last)
                    continued_takers++;
                qty[maker] -= int'(accepted.volume);
                if (qty[maker] == 0)
                    in_book[maker] = 1'b0;
                else
                    partial_makers++;
            end
            REST: begin
                rests++;
                in_book[id] = 1'b1;
                side[id] = accepted.is_ask;
                price[id] = int'(accepted.px);
                qty[id] = int'(accepted.volume);
                stamp[id] = next_stamp++;
            end
            CANCEL: begin
                cancellations++;
                in_book[id] = 1'b0;
            end
            REJECT: rejects++;
            default: $fatal(1, "unknown event");
        endcase
    endtask

    task automatic drive_command(
        input bit cancel, input bit ask, input integer id, input integer px, input integer volume
    );
        event_t expected;
        integer remaining, delay_cycles, steps;
        remaining = volume;
        steps = 0;
        @(negedge clk);
        s_valid = 1'b1;
        s_cancel = cancel;
        s_is_ask = ask;
        s_order_id = IW'(id);
        s_price = PW'(px);
        s_qty = QW'(volume);
        @(posedge clk);
        if (s_ready !== 1'b1 || m_valid !== 1'b0)
            $fatal(1, "failed to accept a command while idle");
        commands++;
        @(negedge clk);
        s_valid = 1'b0;
        // Churn the input pins with valid low while the latched command executes.
        s_order_id = 'x;
        s_price = 'x;
        s_qty = 'x;

        do begin
            steps++;
            if (steps > ORDER_SLOTS)
                $fatal(1, "command exceeded its event bound");
            expected = predict(cancel, ask, id, px, remaining);
            delay_cycles = draw(4);
            repeat (delay_cycles) begin
                @(posedge clk);
                check_event(expected);
                check_book();
                stalls++;
                @(negedge clk);
            end
            m_ready = 1'b1;
            @(posedge clk);
            check_event(expected);
            update_model(expected);
            remaining = int'(expected.left);
            #1;
            check_book();
            if (m_valid !== !expected.last || s_ready !== expected.last)
                $fatal(1, "wrong command state after event %0d", events);
            @(negedge clk);
            m_ready = 1'b0;
        end while (!expected.last);
    endtask

    task automatic reset_engine;
        @(negedge clk);
        rst = 1'b1;
        @(posedge clk);
        for (integer i = 0; i < ORDER_SLOTS; i++)
            in_book[i] = 1'b0;
        #1;
        if (m_valid !== 1'b0 || s_ready !== 1'b0)
            $fatal(1, "pending handshakes during reset");
        check_book();
        @(negedge clk);
        rst = 1'b0;
    endtask

    initial begin : run
        integer id, px, size;
        bit ask, cancel;
        rst = 1'b0;
        s_valid = 1'b0;
        s_cancel = 1'b0;
        s_is_ask = 1'b0;
        s_order_id = '0;
        s_price = '0;
        s_qty = '0;
        m_ready = 1'b0;
        reset_engine();
        drive_command(0, 0, ORDER_SLOTS, 0, 1);
        drive_command(0, 0, 0, PRICE_LEVELS, 1);
        drive_command(0, 0, 0, 0, 0);
        drive_command(1, 1, 0, PRICE_LEVELS, 0);
        drive_command(0, 0, 0, 0, MAX_QTY);
        drive_command(0, 1, 0, 0, 1);
        drive_command(1, 1, 0, PRICE_LEVELS, 0);

        if (ORDER_SLOTS >= 5 && PRICE_LEVELS >= 7) begin
            drive_command(0, 1, 0, 4, 5);
            drive_command(0, 1, 1, 4, 7);
            drive_command(0, 1, 2, 2, 2);
            drive_command(0, 0, 3, 4, 6);  // Price wins, then partial FIFO head fill.
            drive_command(0, 0, 3, 4, 2);  // Earlier head still wins at the same price.
            drive_command(0, 1, 0, 4, 3);  // Reused ID goes to the queue tail.
            drive_command(0, 1, 4, 4, 8);
            drive_command(1, 0, 0, 0, 0);  // Cancel middle.
            drive_command(1, 0, 4, 0, 0);  // Cancel tail.
            drive_command(0, 1, 0, 4, 3);
            drive_command(1, 0, 1, 0, 0);  // Cancel head.
            drive_command(0, 0, 3, 6, 4);  // Fill then rest the incoming remainder.
            drive_command(0, 0, 1, 5, 4);
            drive_command(0, 0, 2, 6, 2);
            drive_command(0, 1, 4, 5, 8);  // Sell sweeps bids in descending price/FIFO.
            drive_command(1, 0, 4, 0, 0);  // Cancel sole occupant.
        end

        if (ORDER_SLOTS > 1 && ORDER_SLOTS <= MAX_QTY) begin
            // Exercise every maker slot except the free incoming ID. The
            // command emits ORDER_SLOTS-1 fills and one terminal rest.
            for (integer i = 0; i < ORDER_SLOTS-1; i++)
                drive_command(0, 1, i, PRICE_LEVELS-1, 1);
            drive_command(0, 0, ORDER_SLOTS-1, PRICE_LEVELS-1, ORDER_SLOTS);
            drive_command(1, 0, ORDER_SLOTS-1, 0, 0);
        end

        // Assert reset with both preexisting liquidity and a stalled event.
        drive_command(0, 1, 0, 0, 1);
        @(negedge clk);
        s_valid = 1'b1;
        s_cancel = 1'b1;
        s_order_id = '0;
        @(posedge clk);
        if (!s_ready)
            $fatal(1, "reset setup did not accept the command");
        @(negedge clk);
        s_valid = 1'b0;
        if (!m_valid || m_kind !== 2'(CANCEL))
            $fatal(1, "expected a stalled cancellation before reset");
        reset_engine();
        drive_command(1, 0, 0, 0, 0);

        for (integer i = 0; i < RANDOM_COMMANDS; i++) begin
            cancel = draw(4) == 0;
            ask = draw(2) == 1;
            id = draw(ORDER_SLOTS + 1);
            px = draw(PRICE_LEVELS + 1);
            size = draw(32);
            drive_command(cancel, ask, id, px, size);
        end
        if (rests == 0 || cancellations == 0 || rejects == 0 || stalls == 0)
            $fatal(1, "missing common event coverage");
        if (ORDER_SLOTS >= 5 && PRICE_LEVELS >= 7 &&
            (fills == 0 || continued_takers == 0 || partial_makers == 0))
            $fatal(1, "missing match coverage");
        $display("PASS matching engine P=%0d N=%0d Q=%0d commands=%0d events=%0d stalled=%0d fills=%0d partial_makers=%0d continued_takers=%0d rests=%0d cancels=%0d rejects=%0d",
                 PRICE_LEVELS, ORDER_SLOTS, QW, commands, events, stalls, fills,
                 partial_makers, continued_takers, rests, cancellations, rejects);
        $finish;
    end

    initial begin
        #1000000;
        $fatal(1, "simulation timeout");
    end
endmodule

`default_nettype wire
