`timescale 1ns/1ps
`default_nettype none
`include "market_codes.svh"

module tb_market_mbo_book;
    localparam integer LEVELS = 5, SLOTS = 4, QTY_W = 4;
    localparam integer PW = $clog2(LEVELS+1), IW = $clog2(SLOTS+1), DW = QTY_W+IW;
    localparam integer MAX_QTY = (1<<QTY_W)-1;

    logic clk = 1'b0;
    logic rst = 1'b0, clear_book = 1'b0, commit = 1'b0;
    logic [3:0] op = '0;
    logic [IW-1:0] id = '0, replacement = '0;
    logic side = 1'b0;
    logic [PW-1:0] price = '0;
    logic [QTY_W-1:0] quantity = '0;
    wire [3:0] error_code;
    wire order_present, order_side, bbo_bid_present, bbo_ask_present;
    wire [PW-1:0] order_price, bbo_bid, bbo_ask;
    wire [QTY_W-1:0] order_quantity;
    wire [DW-1:0] queried_depth, bbo_bid_qty, bbo_ask_qty;
    wire [IW-1:0] queried_count, bbo_bid_count, bbo_ask_count, live_count;

    market_mbo_book #(.LEVELS(LEVELS), .SLOTS(SLOTS), .QTY_W(QTY_W)) dut (.*);

    bit ref_live [0:SLOTS-1];
    bit ref_side [0:SLOTS-1];
    integer ref_price [0:SLOTS-1], ref_qty [0:SLOTS-1];
    integer operations = 0, replace_checks = 0, both_levels_occupied = 0;
    integer withheld = 0, wrong_statuses = 0, snapshots = 0, full_deletes = 0;

    task automatic rising;
        #2;
        clk = 1'b1;
        #2;
        clk = 1'b0;
        #1;
    endtask

    task automatic new_book(input bit power_reset);
        if (power_reset)
            rst = 1'b1;
        else
            clear_book = 1'b1;
        rising();
        rst = 1'b0;
        clear_book = 1'b0;
        for (integer slot = 0; slot < SLOTS; slot++)
            ref_live[slot] = 1'b0;
        snapshots++;
    endtask

    function automatic integer count_px(input bit sell, input integer px);
        integer count;
        count = 0;
        for (integer slot = 0; slot < SLOTS; slot++)
            if (ref_live[slot] && ref_side[slot] == sell && ref_price[slot] == px)
                count++;
        return count;
    endfunction

    function automatic integer sum_px(input bit sell, input integer px);
        integer amount;
        amount = 0;
        for (integer slot = 0; slot < SLOTS; slot++)
            if (ref_live[slot] && ref_side[slot] == sell && ref_price[slot] == px)
                amount += ref_qty[slot];
        return amount;
    endfunction

    task automatic check_slot(input integer slot);
        bit present;
        present = slot < SLOTS && ref_live[slot];
        id = IW'(slot);
        #1;
        if (order_present !== present)
            $fatal(1, "slot %0d wrong presence after operation %0d", slot, operations);
        if (present) begin
            if ({order_side, order_price, order_quantity} !==
                {ref_side[slot], PW'(ref_price[slot]), QTY_W'(ref_qty[slot])})
                $fatal(1, "slot %0d metadata mismatch after operation %0d", slot, operations);
        end else if ({order_side, order_price, order_quantity} !== '0)
            $fatal(1, "invalid slot exposed previous data");
    endtask

    task automatic check_level(input bit sell, input integer px);
        side = sell;
        price = PW'(px);
        #1;
        if (queried_depth !== DW'(sum_px(sell, px)) || queried_count !== IW'(count_px(sell, px)))
            $fatal(1, "side %0d price %0d aggregate after op %0d", sell, px, operations);
    endtask

    task automatic check_best;
        integer bb, ba, count;
        count = 0;
        bb = -1;
        ba = -1;
        for (integer slot = 0; slot < SLOTS; slot++) begin
            if (ref_live[slot]) begin
                count++;
                if (ref_side[slot] && (ba < 0 || ref_price[slot] < ba))
                    ba = ref_price[slot];
                if (!ref_side[slot] && (bb < 0 || ref_price[slot] > bb))
                    bb = ref_price[slot];
            end
        end
        if (live_count !== IW'(count) || bbo_bid_present !== (bb >= 0) || bbo_ask_present !== (ba >= 0))
            $fatal(1, "best selectors or live count mismatch");
        if (bb >= 0) begin
            if (bbo_bid !== PW'(bb) || bbo_bid_qty !== DW'(sum_px(0, bb)) ||
                bbo_bid_count !== IW'(count_px(0, bb)))
                $fatal(1, "bad bid after operation %0d", operations);
        end else if ({bbo_bid, bbo_bid_qty, bbo_bid_count} !== '0)
            $fatal(1, "empty bid carries stale data");
        if (ba >= 0) begin
            if (bbo_ask !== PW'(ba) || bbo_ask_qty !== DW'(sum_px(1, ba)) ||
                bbo_ask_count !== IW'(count_px(1, ba)))
                $fatal(1, "bad ask after operation %0d", operations);
        end else if ({bbo_ask, bbo_ask_qty, bbo_ask_count} !== '0)
            $fatal(1, "empty ask carries stale data");
    endtask

    task automatic verify_memory;
        commit = 1'b0;
        for (integer slot = 0; slot <= SLOTS; slot++)
            check_slot(slot);
        for (integer sell = 0; sell < 2; sell++)
            for (integer px = 0; px <= LEVELS; px++)
                check_level(sell != 0, px);
        check_best();
    endtask

    task automatic step(
        input logic [3:0] operation, input integer source_slot, input integer target_slot,
        input bit sell, input integer new_px, input integer amount,
        input logic [3:0] status, input bit allow
    );
        bit stored_side;
        commit = 1'b0;
        op = operation;
        id = IW'(source_slot);
        replacement = IW'(target_slot);
        side = sell;
        price = PW'(new_px);
        quantity = QTY_W'(amount);
        #1;
        if (error_code !== status)
            $fatal(1, "op %0d source %0d dest %0d expected status %0d got %0d",
                operation, source_slot, target_slot, status, error_code);
        if (status != `ME_OK)
            wrong_statuses++;
        if (status == `ME_OK && !allow)
            withheld++;
        commit = allow && status == `ME_OK;
        rising();

        if (commit) begin
            case (operation)
                `MOP_ADD, `MOP_SNAP_ADD: begin
                    ref_live[source_slot] = 1'b1;
                    ref_side[source_slot] = sell;
                    ref_price[source_slot] = new_px;
                    ref_qty[source_slot] = amount;
                end
                `MOP_REPLACE: begin
                    stored_side = ref_side[source_slot];
                    if (count_px(stored_side, ref_price[source_slot]) > 1 &&
                        count_px(stored_side, new_px) != 0 && new_px != ref_price[source_slot])
                        both_levels_occupied++;
                    ref_live[source_slot] = 1'b0;
                    ref_live[target_slot] = 1'b1;
                    ref_side[target_slot] = stored_side;
                    ref_price[target_slot] = new_px;
                    ref_qty[target_slot] = amount;
                    replace_checks++;
                end
                `MOP_DELETE: begin
                    ref_live[source_slot] = 1'b0;
                    full_deletes++;
                end
                `MOP_CANCEL, `MOP_EXECUTE: begin
                    ref_qty[source_slot] -= amount;
                    if (ref_qty[source_slot] == 0)
                        ref_live[source_slot] = 1'b0;
                end
                default: $fatal(1, "invalid operation at book commit");
            endcase
        end
        operations++;
        verify_memory();
    endtask

    initial begin : exhaustive_replacements
        new_book(1'b1);
        verify_memory();
        step(`MOP_REPLACE, 0, 2, 0, 0, 1, `ME_ABSENT, 0);
        step(`MOP_ADD, SLOTS, 0, 0, LEVELS, 0, `ME_ID, 0);
        for (integer side_value = 0; side_value < 2; side_value++) begin
            for (integer src_price = 0; src_price < LEVELS; src_price++) begin
                for (integer dst_price = 0; dst_price < LEVELS; dst_price++) begin
                    new_book(1'b0);
                    verify_memory();
                    step(`MOP_SNAP_ADD, 0, 0, side_value != 0, src_price, MAX_QTY, `ME_OK, 1);
                    step(`MOP_ADD, 1, 0, side_value != 0, src_price, MAX_QTY, `ME_OK, 1);
                    step(`MOP_ADD, 2, 0, side_value != 0, dst_price, MAX_QTY, `ME_OK, 1);
                    step(`MOP_REPLACE, 0, 1, side_value == 0, dst_price, 2, `ME_DUPLICATE, 0);
                    step(`MOP_REPLACE, 0, 0, side_value == 0, dst_price, 2, `ME_REPLACE_ID, 0);
                    step(`MOP_REPLACE, 0, 3, side_value == 0, dst_price, 2, `ME_OK, 0);
                    step(`MOP_REPLACE, 0, 3, side_value == 0, dst_price, MAX_QTY, `ME_OK, 1);
                    step(`MOP_CANCEL, 3, 0, 0, 0, MAX_QTY, `ME_QTY, 0);
                    step(`MOP_CANCEL, 3, 0, 0, 0, 1, `ME_OK, 1);
                    step(`MOP_EXECUTE, 3, 0, 0, 0, MAX_QTY-1, `ME_OK, 1);
                    step(`MOP_DELETE, 1, 0, 0, 0, 0, `ME_OK, 1);
                    step(`MOP_DELETE, 2, 0, 0, 0, 0, `ME_OK, 1);
                end
            end
        end
        if (replace_checks != 2*LEVELS*LEVELS || both_levels_occupied != 2*LEVELS*(LEVELS-1) ||
            withheld != replace_checks || wrong_statuses < 3*replace_checks || full_deletes != 2*replace_checks)
            $fatal(1, "incomplete replacement/read coverage");
        $display("PASS MBO atomic writes operations=%0d replacements=%0d dual_levels=%0d snapshot_clears=%0d",
            operations, replace_checks, both_levels_occupied, snapshots);
        $finish;
    end
endmodule

`default_nettype wire
