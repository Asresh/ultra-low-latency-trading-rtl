`timescale 1ns/1ps
`default_nettype none

module tb_market_state_quote_engine #(
    parameter integer BANKS = 2,
    parameter integer LEVELS = 16,
    parameter integer SLOTS = 16,
    parameter integer QTY_W = 12,
    parameter integer SEQ_W = 16,
    parameter integer EPOCH_W = 8,
    parameter integer TAG_W = 16,
    parameter integer STAT_W = 32,
    parameter integer REV_W = 16,
    parameter integer MAX_IDLE_TICKS = 16,
    parameter integer QUOTE_SIZE = 12,
    parameter integer HALF_SPREAD = 1,
    parameter integer SKEW_TICKS = 1,
    parameter integer MIN_MARKET_SPREAD = 2,
    parameter integer IMBALANCE_Q8 = 64
);
    localparam integer BANK_W = $clog2(BANKS+1);
    localparam integer PRICE_W = $clog2(LEVELS+1);
    localparam integer ID_W = $clog2(SLOTS+1);
    localparam integer DEPTH_W = QTY_W+ID_W;
    localparam integer DRAIN_CYCLES = 8+3*BANKS;

    logic clk = 1'b0;
    always #5 clk = !clk;
    logic rst = 1'b1;
    logic tick = 1'b0;
    logic s_valid = 1'b0;
    wire s_ready;
    logic [BANK_W-1:0] s_instrument = '0;
    logic [3:0] s_op = '0;
    logic [SEQ_W-1:0] s_seq = '0;
    logic [ID_W-1:0] s_id = '0;
    logic [ID_W-1:0] s_new_id = '0;
    logic s_side = 1'b0;
    logic [PRICE_W-1:0] s_price = '0;
    logic [QTY_W-1:0] s_qty = '0;
    logic [3:0] s_stat_index = '0;
    logic [TAG_W-1:0] s_tag = '0;

    wire r_valid;
    logic r_ready = 1'b0;
    wire [BANK_W-1:0] r_instrument;
    wire [3:0] r_op, r_status;
    wire [SEQ_W-1:0] r_seq, r_next_seq;
    wire [ID_W-1:0] r_id, r_count, r_bid_count, r_ask_count;
    wire [TAG_W-1:0] r_tag;
    wire [1:0] r_mode;
    wire [EPOCH_W-1:0] r_epoch;
    wire r_present, r_side, r_bid_present, r_ask_present;
    wire [PRICE_W-1:0] r_price, r_bid, r_ask;
    wire [DEPTH_W-1:0] r_qty, r_bid_qty, r_ask_qty;
    wire [STAT_W-1:0] r_stat;

    wire q_valid, q_current;
    logic q_ready = 1'b0;
    wire [BANK_W-1:0] q_instrument;
    wire q_active;
    wire [EPOCH_W-1:0] q_epoch;
    wire [REV_W-1:0] q_revision;
    wire [PRICE_W-1:0] q_bid, q_ask;
    wire [QTY_W-1:0] q_size;
    wire [2:0] q_reason;
    wire [BANKS-1:0] o_live, o_eligible, o_up_pressure, o_down_pressure;
    wire [BANKS*EPOCH_W-1:0] o_epoch;

    market_state_quote_engine #(
        .BANKS(BANKS), .LEVELS(LEVELS), .SLOTS(SLOTS), .QTY_W(QTY_W),
        .SEQ_W(SEQ_W), .EPOCH_W(EPOCH_W), .TAG_W(TAG_W), .STAT_W(STAT_W), .REV_W(REV_W),
        .MAX_IDLE_TICKS(MAX_IDLE_TICKS), .QUOTE_SIZE(QUOTE_SIZE), .HALF_SPREAD(HALF_SPREAD),
        .SKEW_TICKS(SKEW_TICKS), .MIN_MARKET_SPREAD(MIN_MARKET_SPREAD), .IMBALANCE_Q8(IMBALANCE_Q8)
    ) dut (.*);

    for (genvar bank = 0; bank < BANKS; bank++) begin : audit
        bit initialized = 1'b0;
        integer age_since_reset = 0;
        always @(posedge clk) begin
            if (rst) begin
                initialized <= 1'b1;
                age_since_reset <= 0;
            end else if (initialized) begin
                verify_tables();
                age_since_reset <= age_since_reset+1;
            end
        end

        task automatic verify_tables;
            logic [DEPTH_W-1:0] sums [0:1][0:LEVELS-1];
            integer counts [0:1][0:LEVELS-1], best [0:1];
            integer live_slots, order_side, order_level;
            logic [QTY_W-1:0] amount;
            live_slots = 0;
            for (integer side = 0; side < 2; side++) begin
                best[side] = -1;
                for (integer level = 0; level < LEVELS; level++) begin
                    sums[side][level] = '0;
                    counts[side][level] = 0;
                end
            end

            for (integer slot = 0; slot < SLOTS; slot++) begin
                if (dut.bank_i[bank].instrument.book.live[slot] === 1'b1) begin
                    amount = dut.bank_i[bank].instrument.book.entry_qty[slot];
                    order_side = int'(dut.bank_i[bank].instrument.book.entry_side[slot]);
                    order_level = int'(dut.bank_i[bank].instrument.book.entry_price[slot]);
                    if ($isunknown(amount) || $isunknown(order_side) || $isunknown(order_level) ||
                        amount == '0 || order_level >= LEVELS || order_side < 0 || order_side > 1)
                        $fatal(1, "bank %0d slot %0d invalid live state", bank, slot);
                    live_slots++;
                    sums[order_side][order_level] += DEPTH_W'(amount);
                    counts[order_side][order_level]++;
                end else if (dut.bank_i[bank].instrument.book.live[slot] !== 1'b0)
                    $fatal(1, "bank %0d slot %0d lost occupancy", bank, slot);
            end
            if (dut.bank_i[bank].instrument.book.live_count !== ID_W'(live_slots))
                $fatal(1, "bank %0d live count mismatch at age %0d", bank, age_since_reset);

            for (integer side = 0; side < 2; side++) begin
                for (integer level = 0; level < LEVELS; level++) begin
                    if (dut.bank_i[bank].instrument.book.occupied[side][level] !== (counts[side][level] > 0))
                        $fatal(1, "bank %0d side %0d level %0d occupancy mismatch", bank, side, level);
                    if (counts[side][level] > 0) begin
                        if (dut.bank_i[bank].instrument.book.depth[side][level] !== sums[side][level] ||
                            dut.bank_i[bank].instrument.book.count[side][level] !== ID_W'(counts[side][level]))
                            $fatal(1, "bank %0d side %0d level %0d aggregate mismatch", bank, side, level);
                        if (side == 0 || best[side] < 0)
                            best[side] = level;
                    end
                end
            end
            if (dut.bank_i[bank].bid_present !== (best[0] >= 0) ||
                dut.bank_i[bank].ask_present !== (best[1] >= 0))
                $fatal(1, "bank %0d BBO presents an empty side", bank);
            if (best[0] >= 0 &&
                (dut.bank_i[bank].bid !== PRICE_W'(best[0]) ||
                 dut.bank_i[bank].bid_qty !== sums[0][best[0]]))
                $fatal(1, "bank %0d bid selector mismatch", bank);
            if (best[1] >= 0 &&
                (dut.bank_i[bank].ask !== PRICE_W'(best[1]) ||
                 dut.bank_i[bank].ask_qty !== sums[1][best[1]]))
                $fatal(1, "bank %0d ask selector mismatch", bank);
            if (o_eligible[bank] &&
                (best[0] < 0 || best[1] < 0 ||
                 dut.bank_i[bank].quote_bid >= PRICE_W'(best[1]) ||
                 dut.bank_i[bank].quote_ask <= PRICE_W'(best[0]) ||
                 dut.bank_i[bank].quote_bid >= dut.bank_i[bank].quote_ask ||
                 dut.bank_i[bank].sized_order == '0))
                $fatal(1, "bank %0d generated non-passive or crossed desired quote", bank);
        endtask
    end

    task automatic write_trace(input integer trace_fd, input integer cycle);
        $fwrite(trace_fd, "%0d %0d %0d %0d %0d %0d %0d ",
            cycle, rst, tick, s_valid, s_ready, r_ready, q_ready);
        $fwrite(trace_fd, "%0d %0d %0d %0d %0d %0d %0d %0d %0d %0d ",
            s_instrument, s_op, s_seq, s_id, s_new_id, s_side, s_price, s_qty, s_stat_index, s_tag);
        $fwrite(trace_fd, "%0d ", r_valid);
        if (r_valid) begin
            $fwrite(trace_fd, "%0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d ",
                r_instrument, r_op, r_seq, r_id, r_tag, r_status, r_mode, r_next_seq,
                r_epoch, r_present, r_side, r_price);
            $fwrite(trace_fd, "%0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d ",
                r_qty, r_count, r_stat, r_bid_present, r_ask_present, r_bid, r_ask,
                r_bid_qty, r_ask_qty, r_bid_count, r_ask_count);
        end else begin
            $fwrite(trace_fd, "0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 ");
        end
        $fwrite(trace_fd, "%0d ", q_valid);
        if (q_valid)
            $fwrite(trace_fd, "%0d %0d %0d %0d %0d %0d %0d %0d ",
                q_instrument, q_active, q_epoch, q_revision, q_bid, q_ask, q_size, q_reason);
        else
            $fwrite(trace_fd, "0 0 0 0 0 0 0 0 ");
        $fwrite(trace_fd, "%0d %0d %0d %0d %0d",
            o_live, o_eligible, o_up_pressure, o_down_pressure, q_current);
        for (integer bank = 0; bank < BANKS; bank++)
            $fwrite(trace_fd, " %0d", o_epoch[bank*EPOCH_W+:EPOCH_W]);
        $fdisplay(trace_fd, "");
    endtask

    task automatic drive_fields(
        input integer bank, op, seq, id, new_id, side, price, qty, stat_index, tag
    );
        s_instrument = BANK_W'(bank);
        s_op = 4'(op);
        s_seq = SEQ_W'(seq);
        s_id = ID_W'(id);
        s_new_id = ID_W'(new_id);
        s_side = side != 0;
        s_price = PRICE_W'(price);
        s_qty = QTY_W'(qty);
        s_stat_index = 4'(stat_index);
        s_tag = TAG_W'(tag);
    endtask

    task automatic exact_cycle_vectors(input string input_path, input string output_path);
        integer input_fd, output_fd, row, cycle, tokens;
        integer reset_in, tick_in, response_ready_in, quote_ready_in, input_valid;
        integer bank, op, seq, id, new_id, side, price, amount, stat, tag;
        input_fd = $fopen(input_path, "r");
        output_fd = $fopen(output_path, "w");
        if (!input_fd || !output_fd)
            $fatal(1, "could not open journal vectors or trace");
        cycle = 0;
        tokens = 16;
        while (tokens == 16) begin
            @(negedge clk);
            tokens = $fscanf(input_fd, "%d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d\n",
                row, reset_in, tick_in, response_ready_in, quote_ready_in, input_valid,
                bank, op, seq, id, new_id, side, price, amount, stat, tag);
            if (tokens == 16) begin
                if (row != cycle)
                    $fatal(1, "journal vector row %0d expected %0d", row, cycle);
                rst = reset_in != 0;
                tick = tick_in != 0;
                r_ready = response_ready_in != 0;
                q_ready = quote_ready_in != 0;
                s_valid = input_valid != 0;
                drive_fields(bank, op, seq, id, new_id, side, price, amount, stat, tag);
                @(posedge clk);
                write_trace(output_fd, cycle);
                cycle++;
            end else if (tokens != -1) begin
                $fatal(1, "bad exact vector row at cycle %0d", cycle);
            end
        end
        $fclose(input_fd);
        $fclose(output_fd);
        $display("PASS journal vector drive cycles=%0d", cycle);
    endtask

    initial begin : feed
        integer commands_fd, schedule_fd, trace_fd, tokens;
        integer cycle, gap, reset_value, tick_value, response_ready, quote_ready;
        integer bank, op, seq, id, new_id, side, price, qty, stat_index, tag;
        integer accepted, responses, quotes, drained;
        bit have_pending, commands_done, schedule_done;
        string command_path, schedule_path, trace_path, exact_path;
        if (!$value$plusargs("trace=%s", trace_path))
            $fatal(1, "missing +trace");
        if ($value$plusargs("cycle_vectors=%s", exact_path)) begin
            exact_cycle_vectors(exact_path, trace_path);
            $finish;
        end
        if (!$value$plusargs("commands=%s", command_path) ||
            !$value$plusargs("schedule=%s", schedule_path))
            $fatal(1, "missing +commands and +schedule or +cycle_vectors");
        commands_fd = $fopen(command_path, "r");
        schedule_fd = $fopen(schedule_path, "r");
        trace_fd = $fopen(trace_path, "w");
        if (!commands_fd || !schedule_fd || !trace_fd)
            $fatal(1, "failed to open command, schedule or trace file");
        cycle = 0;
        gap = 0;
        drained = 0;
        accepted = 0;
        responses = 0;
        quotes = 0;
        while (drained < DRAIN_CYCLES) begin
            @(negedge clk);
            if (!schedule_done) begin
                tokens = $fscanf(schedule_fd, "%d %d %d %d\n",
                    reset_value, tick_value, response_ready, quote_ready);
                if (tokens == 4) begin
                    rst = reset_value != 0;
                    tick = tick_value != 0;
                    r_ready = response_ready != 0;
                    q_ready = quote_ready != 0;
                end else if (tokens == -1) begin
                    schedule_done = 1'b1;
                end else begin
                    $fatal(1, "bad schedule row at cycle %0d", cycle);
                end
            end
            if (schedule_done) begin
                rst = 1'b0;
                tick = 1'b0;
                r_ready = 1'b1;
                q_ready = 1'b1;
            end
            if (!have_pending && !commands_done) begin
                tokens = $fscanf(commands_fd, "%d %d %d %d %d %d %d %d %d %d %d\n",
                    bank, op, seq, id, new_id, side, price, qty, stat_index, tag, gap);
                if (tokens == 11)
                    have_pending = 1'b1;
                else if (tokens == -1)
                    commands_done = 1'b1;
                else
                    $fatal(1, "bad command row after %0d accepted commands", accepted);
            end
            s_valid = have_pending && !rst && gap == 0;
            if (have_pending && !rst && gap != 0)
                gap--;
            drive_fields(bank, op, seq, id, new_id, side, price, qty, stat_index, tag);
            @(posedge clk);
            if (cycle >= 400000)
                $fatal(1, "simulation exceeded cycle limit");
            write_trace(trace_fd, cycle);
            if (s_valid && s_ready) begin
                have_pending = 1'b0;
                accepted++;
            end
            if (r_valid && r_ready)
                responses++;
            if (q_valid && q_ready)
                quotes++;
            if (!have_pending && commands_done && schedule_done)
                drained++;
            cycle++;
        end
        $fclose(commands_fd);
        $fclose(schedule_fd);
        $fclose(trace_fd);
        $display("PASS market engine banks=%0d levels=%0d slots=%0d accepts=%0d responses=%0d quotes=%0d",
            BANKS, LEVELS, SLOTS, accepted, responses, quotes);
        $finish;
    end
endmodule

`default_nettype wire
