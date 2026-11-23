`timescale 1ns/1ps
`default_nettype none
`include "market_codes.svh"

module market_instrument #(
    parameter integer LEVELS = 16,
    parameter integer SLOTS = 16,
    parameter integer QTY_W = 12,
    parameter integer SEQ_W = 16,
    parameter integer EPOCH_W = 8,
    parameter integer STAT_W = 32,
    parameter integer MAX_IDLE_TICKS = 16,
    localparam integer PRICE_W = $clog2(LEVELS + 1),
    localparam integer ID_W = $clog2(SLOTS + 1),
    localparam integer DEPTH_W = QTY_W + ID_W,
    localparam integer AGE_W = $clog2(MAX_IDLE_TICKS + 1)
) (
    input wire clk,
    input wire rst,
    input wire tick,
    input wire accept,
    input wire [3:0] op,
    input wire [SEQ_W-1:0] sequence_in,
    input wire [ID_W-1:0] id,
    input wire [ID_W-1:0] replacement,
    input wire side,
    input wire [PRICE_W-1:0] price,
    input wire [QTY_W-1:0] quantity,
    input wire [3:0] stat_index,
    output wire live,
    output logic [EPOCH_W-1:0] epoch,
    output logic [3:0] rsp_status,
    output logic [1:0] rsp_mode,
    output logic [SEQ_W-1:0] rsp_expected,
    output logic [EPOCH_W-1:0] rsp_epoch,
    output logic rsp_present,
    output logic rsp_side,
    output logic [PRICE_W-1:0] rsp_price,
    output logic [DEPTH_W-1:0] rsp_qty,
    output logic [ID_W-1:0] rsp_count,
    output logic [STAT_W-1:0] rsp_stat,
    output wire bid_present,
    output wire ask_present,
    output wire [PRICE_W-1:0] bid_price,
    output wire [PRICE_W-1:0] ask_price,
    output wire [DEPTH_W-1:0] bid_qty,
    output wire [DEPTH_W-1:0] ask_qty,
    output wire [ID_W-1:0] bid_count,
    output wire [ID_W-1:0] ask_count
);
    logic [1:0] mode;
    logic [SEQ_W-1:0] expected;
    logic [AGE_W-1:0] age;
    logic [STAT_W-1:0] commands, updates, gaps, faults, timeouts, snapshots;
    logic begin_snapshot, mutate, stream_progress, stream_fault;
    wire [3:0] book_error;
    wire order_present, order_side;
    wire [PRICE_W-1:0] order_price;
    wire [QTY_W-1:0] order_quantity;
    wire [DEPTH_W-1:0] level_depth;
    wire [ID_W-1:0] level_count, live_count;

    wire query = op >= `MOP_LEVEL && op <= `MOP_STAT;
    wire normal_message = op <= `MOP_HEARTBEAT;
    wire snapshot_message = op == `MOP_SNAP_ADD || op == `MOP_SNAP_END;
    wire mutation_message = op <= `MOP_REPLACE || op == `MOP_SNAP_ADD;
    wire correct_mode = (normal_message && mode == `MS_LIVE) ||
                        (snapshot_message && mode == `MS_BOOT);
    wire refresh = accept && (begin_snapshot || stream_progress);
    wire fault_now = accept && stream_fault;
    wire elapsed = tick && mode == `MS_LIVE && !refresh && !fault_now;
    wire expires = elapsed && (age == AGE_W'(MAX_IDLE_TICKS-1));

    assign live = !rst && mode == `MS_LIVE;

    market_mbo_book #(.LEVELS(LEVELS), .SLOTS(SLOTS), .QTY_W(QTY_W)) book (
        .clk(clk), .rst(rst), .clear_book(accept && begin_snapshot), .commit(accept && mutate),
        .op(op), .id(id), .replacement(replacement), .side(side), .price(price), .quantity(quantity),
        .error_code(book_error), .order_present(order_present), .order_side(order_side),
        .order_price(order_price), .order_quantity(order_quantity),
        .queried_depth(level_depth), .queried_count(level_count),
        .bbo_bid_present(bid_present), .bbo_ask_present(ask_present),
        .bbo_bid(bid_price), .bbo_ask(ask_price), .bbo_bid_qty(bid_qty), .bbo_ask_qty(ask_qty),
        .bbo_bid_count(bid_count), .bbo_ask_count(ask_count), .live_count(live_count)
    );

    always_comb begin
        rsp_status = `ME_OK;
        rsp_mode = mode;
        rsp_expected = expected;
        rsp_epoch = epoch;
        rsp_present = 1'b0;
        rsp_side = 1'b0;
        rsp_price = '0;
        rsp_qty = '0;
        rsp_count = '0;
        rsp_stat = '0;
        begin_snapshot = 1'b0;
        mutate = 1'b0;
        stream_progress = 1'b0;
        stream_fault = 1'b0;

        if (query) begin
            case (op)
                `MOP_LEVEL: begin
                    if (price >= PRICE_W'(LEVELS)) begin
                        rsp_status = `ME_PRICE;
                    end else begin
                        rsp_present = (level_count != '0);
                        rsp_side = side;
                        rsp_price = price;
                        rsp_qty = level_depth;
                        rsp_count = level_count;
                    end
                end
                `MOP_ORDER: begin
                    if (id >= ID_W'(SLOTS)) begin
                        rsp_status = `ME_ID;
                    end else begin
                        rsp_present = order_present;
                        rsp_side = order_side;
                        rsp_price = order_price;
                        rsp_qty = DEPTH_W'(order_quantity);
                    end
                end
                `MOP_STAT: begin
                    case (stat_index)
                        4'd`STAT_COMMANDS: rsp_stat = commands;
                        4'd`STAT_UPDATES: rsp_stat = updates;
                        4'd`STAT_GAPS: rsp_stat = gaps;
                        4'd`STAT_FAULTS: rsp_stat = faults;
                        4'd`STAT_TIMEOUTS: rsp_stat = timeouts;
                        4'd`STAT_SNAPSHOTS: rsp_stat = snapshots;
                        4'd`STAT_LIVE_ORDERS: rsp_stat = STAT_W'(live_count);
                        4'd`STAT_AGE: rsp_stat = STAT_W'(age);
                        default: rsp_status = `ME_STAT;
                    endcase
                end
                default: ;
            endcase
        end else if (op == `MOP_SNAP_BEGIN) begin
            begin_snapshot = 1'b1;
            rsp_mode = `MS_BOOT;
            rsp_expected = sequence_in + SEQ_W'(1);
            rsp_epoch = epoch + EPOCH_W'(1);
        end else if (!normal_message && !snapshot_message) begin
            rsp_status = `ME_CODE;
            stream_fault = mode == `MS_LIVE || mode == `MS_BOOT;
        end else if (!correct_mode) begin
            rsp_status = `ME_MODE;
        end else if (sequence_in != expected) begin
            rsp_status = `ME_GAP;
            stream_fault = 1'b1;
        end else if (mutation_message && book_error != `ME_OK) begin
            rsp_status = book_error;
            stream_fault = 1'b1;
        end else begin
            stream_progress = 1'b1;
            mutate = mutation_message;
            rsp_expected = expected + SEQ_W'(1);
            if (op == `MOP_SNAP_END)
                rsp_mode = `MS_LIVE;
        end
        if (stream_fault)
            rsp_mode = `MS_FENCED;
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            mode <= `MS_COLD;
            epoch <= '0;
            expected <= '0;
            age <= '0;
            commands <= '0;
            updates <= '0;
            gaps <= '0;
            faults <= '0;
            timeouts <= '0;
            snapshots <= '0;
        end else begin
            if (accept) begin
                commands <= commands + STAT_W'(1);
                if (begin_snapshot || stream_progress || stream_fault) begin
                    mode <= rsp_mode;
                    epoch <= rsp_epoch;
                    expected <= rsp_expected;
                end
                if (mutate)
                    updates <= updates + STAT_W'(1);
                if (stream_fault && rsp_status == `ME_GAP)
                    gaps <= gaps + STAT_W'(1);
                if (stream_fault && rsp_status != `ME_GAP)
                    faults <= faults + STAT_W'(1);
                if (stream_progress && op == `MOP_SNAP_END)
                    snapshots <= snapshots + STAT_W'(1);
            end
            if (refresh) begin
                age <= '0;
            end else if (elapsed) begin
                age <= age + AGE_W'(1);
                if (expires) begin
                    mode <= `MS_FENCED;
                    timeouts <= timeouts + STAT_W'(1);
                end
            end
        end
    end
endmodule

`default_nettype wire
