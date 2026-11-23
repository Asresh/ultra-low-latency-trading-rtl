`timescale 1ns/1ps
`default_nettype none

module tb_banked_matching_engine #(
    parameter integer BANKS = 3,
    parameter integer PRICE_LEVELS = 12,
    parameter integer ORDER_SLOTS = 12,
    parameter integer QTY_W = 16,
    parameter integer CMD_DEPTH = 4,
    parameter integer EVENT_DEPTH = 4,
    parameter integer SEQ_W = 8,
    parameter integer TAG_W = 16
);
    localparam integer BW = $clog2(BANKS + 1);
    localparam integer PW = $clog2(PRICE_LEVELS + 1);
    localparam integer IW = $clog2(ORDER_SLOTS + 1);
    localparam integer LQW = QTY_W + IW;
    localparam integer DRAIN_LIMIT = (BANKS + 1) * (CMD_DEPTH + EVENT_DEPTH + 2) *
                                    (ORDER_SLOTS + 2) + 12;

    logic clk = 1'b0;
    always #5 clk = !clk;
    logic rst = 1'b1;
    logic s_valid = 1'b0;
    wire s_ready;
    logic [BW-1:0] s_instrument = '0;
    logic [1:0] s_opcode = '0;
    logic [1:0] s_tif = '0;
    logic s_is_ask = 1'b0;
    logic [IW-1:0] s_order_id = '0;
    logic [PW-1:0] s_price = '0;
    logic [QTY_W-1:0] s_qty = '0;
    logic [TAG_W-1:0] s_tag = '0;
    wire m_valid;
    logic m_ready = 1'b0;
    wire [BW-1:0] m_instrument;
    wire [2:0] m_kind;
    wire m_last, m_is_ask;
    wire [IW-1:0] m_order_id, m_maker_id, m_level_count;
    wire [PW-1:0] m_price;
    wire [QTY_W-1:0] m_qty, m_remaining;
    wire [3:0] m_reject;
    wire [LQW-1:0] m_level_qty;
    wire [TAG_W-1:0] m_tag;
    wire [SEQ_W-1:0] m_cmd_seq, m_event_seq;

    banked_matching_engine #(
        .BANKS(BANKS), .PRICE_LEVELS(PRICE_LEVELS), .ORDER_SLOTS(ORDER_SLOTS),
        .QTY_W(QTY_W), .TAG_W(TAG_W), .SEQ_W(SEQ_W),
        .CMD_DEPTH(CMD_DEPTH), .EVENT_DEPTH(EVENT_DEPTH)
    ) dut (.*);

    wire [BANKS-1:0] starts, commits;
    wire [(BANKS+1)*16-1:0] event_used;
    wire [BANKS*16-1:0] command_used;
    for (genvar bank = 0; bank < BANKS; bank++) begin : gen_observe_bank
        assign starts[bank] = dut.gen_bank[bank].bank.cmd_ready && dut.gen_bank[bank].bank.cmd_valid;
        assign commits[bank] = dut.gen_bank[bank].bank.book_ready && dut.gen_bank[bank].bank.book_valid;
        assign command_used[16*bank+:16] = 16'(dut.gen_bank[bank].bank.command_fifo.used);
        assign event_used[16*bank+:16] = 16'(dut.gen_bank[bank].bank.event_fifo.used);

        bit initialized = 1'b0;
        always @(posedge clk) begin
            if (rst)
                initialized <= 1'b1;
            else if (initialized)
                check_slot_links();
        end

        task automatic check_slot_links;
            bit [ORDER_SLOTS-1:0] seen;
            integer node, previous, best_bid, best_ask, listed, sum_qty;
            bit occupied;
            best_bid = -1;
            best_ask = PRICE_LEVELS;
            seen = '0;
            for (integer side = 0; side < 2; side++) begin
                for (integer p = 0; p < PRICE_LEVELS; p++) begin
                    occupied = (side == 0)
                        ? dut.gen_bank[bank].bank.book.bid_occupied[p]
                        : dut.gen_bank[bank].bank.book.ask_occupied[p];
                    if (occupied) begin
                        if (side == 0 && p > best_bid)
                            best_bid = p;
                        if (side == 1 && p < best_ask)
                            best_ask = p;
                        node = int'(dut.gen_bank[bank].bank.book.head_id[side][p]);
                        previous = ORDER_SLOTS;
                        listed = 0;
                        sum_qty = 0;
                        if (node == ORDER_SLOTS)
                            $fatal(1, "bank %0d occupied level %0d/%0d has no head", bank, side, p);
                        while (node != ORDER_SLOTS) begin
                            if ($isunknown(node) || node < 0 || node >= ORDER_SLOTS)
                                $fatal(1, "bank %0d bad pointer %0d", bank, node);
                            if (seen[node] || dut.gen_bank[bank].bank.book.live[node] !== 1'b1)
                                $fatal(1, "bank %0d repeated or unowned slot %0d", bank, node);
                            if (dut.gen_bank[bank].bank.book.order_is_ask[node] !== (side == 1) ||
                                dut.gen_bank[bank].bank.book.order_price[node] !== PW'(p))
                                $fatal(1, "bank %0d wrong queue for slot %0d", bank, node);
                            if (dut.gen_bank[bank].bank.book.previous_id[node] !== IW'(previous))
                                $fatal(1, "bank %0d bad backlink from slot %0d", bank, node);
                            if (dut.gen_bank[bank].bank.book.order_qty[node] == 0)
                                $fatal(1, "bank %0d zero quantity live slot %0d", bank, node);
                            seen[node] = 1'b1;
                            listed++;
                            sum_qty += int'(dut.gen_bank[bank].bank.book.order_qty[node]);
                            previous = node;
                            node = int'(dut.gen_bank[bank].bank.book.next_id[node]);
                        end
                        if (dut.gen_bank[bank].bank.book.tail_id[side][p] !== IW'(previous))
                            $fatal(1, "bank %0d wrong tail at %0d/%0d", bank, side, p);
                        if (dut.gen_bank[bank].bank.book.level_qty[side][p] !== LQW'(sum_qty) ||
                            dut.gen_bank[bank].bank.book.level_count[side][p] !== IW'(listed))
                            $fatal(1, "bank %0d aggregate mismatch at %0d/%0d", bank, side, p);
                    end
                end
            end
            if (seen !== dut.gen_bank[bank].bank.book.live)
                $fatal(1, "bank %0d unreachable order slot", bank);
            if (best_bid >= best_ask)
                $fatal(1, "bank %0d crossed resting book", bank);
            if (dut.gen_bank[bank].bank.book.bid_valid !== (best_bid >= 0) ||
                dut.gen_bank[bank].bank.book.bid_price !== PW'((best_bid >= 0) ? best_bid : 0))
                $fatal(1, "bank %0d best bid tree mismatch", bank);
            if (dut.gen_bank[bank].bank.book.ask_valid !== (best_ask < PRICE_LEVELS) ||
                dut.gen_bank[bank].bank.book.ask_price !== PW'((best_ask < PRICE_LEVELS) ? best_ask : 0))
                $fatal(1, "bank %0d best ask tree mismatch", bank);
        endtask
    end
    assign event_used[16*BANKS+:16] = 16'(dut.invalid_instrument.used);

    task automatic trace_cycle(input integer fd, input integer cycle);
        $fwrite(fd, "%0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d ",
            cycle, rst, s_valid, s_ready, s_instrument, s_opcode, s_tif, s_is_ask,
            s_order_id, s_price, s_qty);
        $fwrite(fd, "%0d %0d %0d ", s_tag, m_valid, m_ready);
        if (m_valid)
            $fwrite(fd, "%0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d ",
                m_instrument, m_tag, m_cmd_seq, m_event_seq, m_kind, m_last, m_order_id,
                m_is_ask, m_maker_id, m_price, m_qty, m_remaining, m_reject,
                m_level_qty, m_level_count);
        else
            $fwrite(fd, "0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 ");
        $fwrite(fd, "%0d %0d %0d %0d",
            dut.lane_m_valid, dut.lane_m_ready, starts, commits);
        for (integer b = 0; b < BANKS; b++)
            $fwrite(fd, " %0d", command_used[b*16+:16]);
        for (integer b = 0; b <= BANKS; b++)
            $fwrite(fd, " %0d", event_used[b*16+:16]);
        $fdisplay(fd, "");
    endtask

    initial begin : drive
        integer command_fd, schedule_fd, trace_fd;
        integer cycle, loaded, gap, drained, r, sink;
        integer bank, op, tif, side, id, price, qty, tag;
        integer accepted, emitted;
        bit have_command, done_commands, done_schedule;
        string command_path, schedule_path, trace_path;
        if (!$value$plusargs("commands=%s", command_path) ||
            !$value$plusargs("schedule=%s", schedule_path) ||
            !$value$plusargs("trace=%s", trace_path))
            $fatal(1, "missing +commands, +schedule or +trace");
        command_fd = $fopen(command_path, "r");
        schedule_fd = $fopen(schedule_path, "r");
        trace_fd = $fopen(trace_path, "w");
        if (!command_fd || !schedule_fd || !trace_fd)
            $fatal(1, "unable to open trace files");
        cycle = 0;
        gap = 0;
        drained = 0;
        accepted = 0;
        emitted = 0;
        while (drained < DRAIN_LIMIT) begin
            @(negedge clk);
            if (!done_schedule) begin
                loaded = $fscanf(schedule_fd, "%d %d\n", r, sink);
                if (loaded == 2) begin
                    rst = (r != 0);
                    m_ready = (sink != 0);
                end else if (loaded == -1)
                    done_schedule = 1'b1;
                else
                    $fatal(1, "invalid schedule row at cycle %0d", cycle);
            end
            if (done_schedule) begin
                rst = 1'b0;
                m_ready = 1'b1;
            end
            if (!have_command && !done_commands) begin
                loaded = $fscanf(command_fd, "%d %d %d %d %d %d %d %d %d\n",
                    bank, op, tif, side, id, price, qty, tag, gap);
                if (loaded == 9)
                    have_command = 1'b1;
                else if (loaded == -1)
                    done_commands = 1'b1;
                else
                    $fatal(1, "invalid command row after %0d accepted inputs", accepted);
            end
            s_valid = have_command && !rst && (gap == 0);
            if (have_command && gap > 0 && !rst)
                gap--;
            s_instrument = BW'(bank);
            s_opcode = 2'(op);
            s_tif = 2'(tif);
            s_is_ask = (side != 0);
            s_order_id = IW'(id);
            s_price = PW'(price);
            s_qty = QTY_W'(qty);
            s_tag = TAG_W'(tag);
            @(posedge clk);
            if (cycle >= 250000)
                $fatal(1, "driver exceeded cycle limit");
            trace_cycle(trace_fd, cycle);
            if (s_valid && s_ready) begin
                accepted++;
                have_command = 1'b0;
            end
            if (m_valid && m_ready)
                emitted++;
            if (done_commands && done_schedule && !have_command)
                drained++;
            cycle++;
        end
        $fclose(command_fd);
        $fclose(schedule_fd);
        $fclose(trace_fd);
        $display("PASS simulation profile banks=%0d command accepts=%0d output transfers=%0d",
            BANKS, accepted, emitted);
        $finish;
    end
endmodule

`default_nettype wire
