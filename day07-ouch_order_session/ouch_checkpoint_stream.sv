`timescale 1ns/1ps
`default_nettype none

// Offline checkpoint transport. A complete image is staged and validated
// before the maintenance gate opens any write port to the native state.
module ouch_checkpoint_stream #(
    parameter integer ORDER_SLOTS = 8
) (
    input wire clk, rst,
    input wire quiescent,
    input wire allow_export, allow_import,
    input wire cp_cmd_valid,
    output wire cp_cmd_ready,
    input wire [1:0] cp_cmd_op,
    input wire [63:0] cp_image_id,
    input wire cp_abort_import,

    input wire cp_in_valid,
    output wire cp_in_ready,
    input wire [31:0] cp_in_data,
    input wire cp_in_last,

    output wire cp_out_valid,
    input wire cp_out_ready,
    output logic [31:0] cp_out_data,
    output wire cp_out_last,

    output wire cp_result_valid,
    input wire cp_result_ready,
    output logic [3:0] cp_result_status,
    output logic [1:0] cp_result_op,
    output logic [63:0] cp_result_id,
    output logic [7:0] cp_bad_slot,

    output wire checkpoint_busy,
    output logic boot_open,
    output wire block_session,
    input ouch_wire_pkg::checkpoint_meta_t current_meta,
    output wire [7:0] inspect_slot,
    input ouch_wire_pkg::order_state_t inspected_order,
    output wire restore_slot_valid,
    output wire [7:0] restore_slot,
    output ouch_wire_pkg::order_state_t restore_order,
    output wire restore_meta_valid,
    output ouch_wire_pkg::checkpoint_meta_t restore_meta
);
    import ouch_wire_pkg::*;
    localparam integer METADATA_BITS = $bits(checkpoint_meta_t);
    localparam integer ORDER_BITS = $bits(order_state_t);
    localparam integer META_WORDS = (METADATA_BITS+31)/32;
    localparam integer ORDER_WORDS = (ORDER_BITS+31)/32;
    localparam integer META_CAPACITY = META_WORDS*32;
    localparam integer ROW_CAPACITY = ORDER_WORDS*32;
    localparam integer FIRST_META = 4, FIRST_ROW = FIRST_META + META_WORDS;
    localparam integer FOOTER = FIRST_ROW + ORDER_SLOTS*ORDER_WORDS;
    localparam integer CURSOR_W = $clog2(FOOTER+1);
    localparam integer ROW_W = ORDER_SLOTS <= 1 ? 1 : $clog2(ORDER_SLOTS);
    localparam logic [31:0] MAGIC = 32'h3150_434f;
    localparam logic [31:0] GEOMETRY = {16'(ORDER_WORDS),8'(META_WORDS),8'(ORDER_SLOTS)};
    localparam logic [31:0] META_PAD = '1 << (METADATA_BITS % 32);
    localparam logic [31:0] ORDER_PAD = '1 << (ORDER_BITS % 32);
    localparam logic [1:0] EXPORT = 0, IMPORT = 1, FRESH = 2;
    localparam logic [3:0] OK = 0, UNSAFE = 1, BAD_CMD = 2, BAD_FRAME = 3,
        BAD_FORMAT = 4, BAD_CRC = 5, BAD_STATE = 6, ABORTED = 7, BAD_ID = 8;
    localparam logic [3:0] IDLE = 0, SEND = 1, RECEIVE = 2, DRAIN = 3,
        READ_ROW = 4, SCAN_REFS = 5, APPLY = 6, INSTALL = 7, RESULT = 8;

    logic [3:0] state;
    logic [3:0] malformed;
    logic [CURSOR_W-1:0] cursor;
    logic [31:0] crc;
    logic [7:0] slot_at, compare_at;
    logic [META_CAPACITY-1:0] metadata;
    logic [ROW_CAPACITY-1:0] staged_rows [0:ORDER_SLOTS-1];
    checkpoint_meta_t staged_meta;
    order_state_t scan_row, compare_row;
    wire [ROW_W-1:0] slot_index = ORDER_SLOTS == 1 ? '0 : ROW_W'(slot_at);
    wire [ROW_W-1:0] compare_index = ORDER_SLOTS == 1 ? '0 : ROW_W'(compare_at);
    wire [ROW_CAPACITY-1:0] read_payload = ROW_CAPACITY'(inspected_order);
    wire in_meta = cursor >= CURSOR_W'(FIRST_META) && cursor < CURSOR_W'(FIRST_ROW);
    wire in_slot = cursor >= CURSOR_W'(FIRST_ROW) && cursor < CURSOR_W'(FOOTER);
    wire footer = cursor == CURSOR_W'(FOOTER);
    wire [31:0] flat = 32'(cursor) - 32'(FIRST_ROW);
    wire [31:0] row_word = flat % 32'(ORDER_WORDS);
    wire [ROW_W-1:0] import_slot = ORDER_SLOTS == 1 ? '0 : ROW_W'(flat / 32'(ORDER_WORDS));
    wire approved_op = cp_cmd_op == EXPORT || cp_cmd_op == IMPORT || cp_cmd_op == FRESH;
    wire command_id_ok = cp_cmd_op == FRESH ? cp_image_id == 0 : cp_image_id != 0;
    wire good_session = valid_session(staged_meta.session_id, 1'b0) &&
                        staged_meta.next_sequence != 0 && !(staged_meta.ended && staged_meta.market_open);

    assign cp_cmd_ready = !rst && state == IDLE;
    assign cp_out_valid = !rst && state == SEND;
    assign cp_out_last = cp_out_valid && footer;
    assign cp_in_ready = !rst && !cp_abort_import && (state == RECEIVE || state == DRAIN);
    assign cp_result_valid = !rst && state == RESULT;
    assign checkpoint_busy = state != IDLE && state != RESULT;
    assign block_session = !boot_open || checkpoint_busy;
    assign inspect_slot = in_slot ? 8'(import_slot) : 8'd0;
    assign restore_slot_valid = !rst && state == APPLY;
    assign restore_slot = slot_at;
    assign scan_row = staged_rows[slot_index][ORDER_BITS-1:0];
    assign compare_row = staged_rows[compare_index][ORDER_BITS-1:0];
    assign restore_order = scan_row;
    assign staged_meta = metadata[METADATA_BITS-1:0];
    assign restore_meta = staged_meta;
    assign restore_meta_valid = !rst && state == INSTALL;

    function automatic logic [31:0] crc_advance(input logic [31:0] running, word_value);
        logic [31:0] result;
        result = running ^ word_value;
        for (integer bit_at = 0; bit_at < 32; bit_at++)
            result = (result >> 1) ^ (result[0] ? 32'h82f6_3b78 : 32'd0);
        return result;
    endfunction

    function automatic logic same_ref(input logic [31:0] first, second);
        return first != 0 && first == second;
    endfunction

    wire conflict = same_ref(scan_row.view.order_ref, compare_row.view.order_ref) ||
        same_ref(scan_row.view.order_ref, compare_row.view.next_order_ref) ||
        same_ref(scan_row.view.next_order_ref, compare_row.view.order_ref) ||
        same_ref(scan_row.view.next_order_ref, compare_row.view.next_order_ref);
    wire row_valid;

    ouch_checkpoint_validate validate (
        .slot(slot_at), .highest_ref(staged_meta.highest_order_ref), .image(scan_row), .valid(row_valid)
    );

    always @* begin
        cp_out_data = '0;
        if (cp_out_valid) begin
            if (cursor == 0)
                cp_out_data = MAGIC;
            else if (cursor == 1)
                cp_out_data = GEOMETRY;
            else if (cursor == 2)
                cp_out_data = cp_result_id[31:0];
            else if (cursor == 3)
                cp_out_data = cp_result_id[63:32];
            else if (in_meta)
                cp_out_data = metadata[(32'(cursor)-32'(FIRST_META))*32 +: 32];
            else if (in_slot)
                cp_out_data = read_payload[row_word*32 +: 32];
            else
                cp_out_data = ~crc;
        end
    end

    logic [3:0] malformed_now;
    always @* begin
        malformed_now = malformed;
        if (malformed == OK) begin
            if ((cursor == 0 && cp_in_data != MAGIC) || (cursor == 1 && cp_in_data != GEOMETRY))
                malformed_now = BAD_FORMAT;
            else if ((cursor == 2 && cp_in_data != cp_result_id[31:0]) ||
                     (cursor == 3 && cp_in_data != cp_result_id[63:32]))
                malformed_now = BAD_ID;
            else if ((METADATA_BITS % 32 != 0 && cursor == CURSOR_W'(FIRST_ROW-1) &&
                      (cp_in_data & META_PAD) != 0) ||
                     (ORDER_BITS % 32 != 0 && in_slot && row_word == 32'(ORDER_WORDS-1) &&
                      (cp_in_data & ORDER_PAD) != 0))
                malformed_now = BAD_FORMAT;
        end
    end

    task automatic complete(input logic [3:0] status);
        begin
            cp_result_status <= status;
            state <= RESULT;
        end
    endtask

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= IDLE;
            boot_open <= 1'b0;
            cursor <= 0;
            malformed <= OK;
            crc <= '1;
            slot_at <= 0;
            compare_at <= 0;
            cp_result_status <= OK;
            cp_result_op <= '0;
            cp_result_id <= '0;
            cp_bad_slot <= 8'hff;
        end else begin
            case (state)
                IDLE: if (cp_cmd_valid) begin
                    cp_result_op <= cp_cmd_op;
                    cp_result_id <= cp_image_id;
                    cp_bad_slot <= 8'hff;
                    cursor <= 0;
                    crc <= '1;
                    malformed <= OK;
                    if (!approved_op || !command_id_ok)
                        complete(BAD_CMD);
                    else if (!quiescent || (cp_cmd_op == EXPORT && (!boot_open || !allow_export)) ||
                             (cp_cmd_op != EXPORT && (boot_open || !allow_import)))
                        complete(UNSAFE);
                    else if (cp_cmd_op == FRESH) begin
                        boot_open <= 1'b1;
                        complete(OK);
                    end else if (cp_cmd_op == EXPORT) begin
                        metadata <= META_CAPACITY'(current_meta);
                        state <= SEND;
                    end else begin
                        state <= RECEIVE;
                    end
                end
                SEND: if (cp_out_ready) begin
                    if (footer)
                        complete(OK);
                    else begin
                        crc <= crc_advance(crc, cp_out_data);
                        cursor <= cursor + 1'b1;
                    end
                end
                RECEIVE: if (cp_abort_import)
                    complete(ABORTED);
                else if (cp_in_valid) begin
                    if (cp_in_last && !footer)
                        complete(BAD_FRAME);
                    else if (footer) begin
                        if (!cp_in_last)
                            state <= DRAIN;
                        else if (malformed != OK)
                            complete(malformed);
                        else if (cp_in_data != ~crc)
                            complete(BAD_CRC);
                        else if (!good_session)
                            complete(BAD_STATE);
                        else begin
                            slot_at <= 0;
                            state <= READ_ROW;
                        end
                    end else begin
                        malformed <= malformed_now;
                        crc <= crc_advance(crc, cp_in_data);
                        cursor <= cursor + 1'b1;
                        if (in_meta)
                            metadata[(32'(cursor)-32'(FIRST_META))*32 +: 32] <= cp_in_data;
                        if (in_slot)
                            staged_rows[import_slot][row_word*32 +: 32] <= cp_in_data;
                    end
                end
                DRAIN: if (cp_abort_import)
                    complete(ABORTED);
                else if (cp_in_valid && cp_in_last)
                    complete(BAD_FRAME);
                READ_ROW: if (!row_valid) begin
                    cp_bad_slot <= slot_at;
                    complete(BAD_STATE);
                end else if (slot_at == 8'(ORDER_SLOTS-1)) begin
                    slot_at <= 0;
                    state <= APPLY;
                end else begin
                    compare_at <= slot_at + 1'b1;
                    state <= SCAN_REFS;
                end
                SCAN_REFS: if (conflict) begin
                    cp_bad_slot <= compare_at;
                    complete(BAD_STATE);
                end else if (compare_at == 8'(ORDER_SLOTS-1)) begin
                    slot_at <= slot_at + 1'b1;
                    state <= READ_ROW;
                end else begin
                    compare_at <= compare_at + 1'b1;
                end
                APPLY: if (slot_at == 8'(ORDER_SLOTS-1))
                    state <= INSTALL;
                else
                    slot_at <= slot_at + 1'b1;
                INSTALL: begin
                    boot_open <= 1'b1;
                    complete(OK);
                end
                RESULT: if (cp_result_ready)
                    state <= IDLE;
                default: state <= IDLE;
            endcase
        end
    end

`ifdef FORMAL
    logic initialized = 1'b0;
    always @(posedge clk) begin
        initialized <= 1'b1;
        if (initialized && !rst && !$past(rst)) begin
            if ($past(cp_out_valid && !cp_out_ready && in_slot))
                assume($stable(inspected_order));
            if ($past(cp_out_valid && !cp_out_ready))
                assert(cp_out_valid && $stable({cp_out_data,cp_out_last}));
            if ($past(cp_result_valid && !cp_result_ready))
                assert(cp_result_valid && $stable({cp_result_status,cp_result_op,cp_result_id,cp_bad_slot}));
            if (restore_slot_valid || restore_meta_valid)
                assert(block_session && !boot_open);
            if ($past(boot_open))
                assert(boot_open && !restore_slot_valid && !restore_meta_valid);
        end
    end
`endif
endmodule

`default_nettype wire
