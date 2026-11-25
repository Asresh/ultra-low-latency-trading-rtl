`timescale 1ns/1ps
`include "quote_codes.svh"

// The store owns one replaceable quote per (instrument, venue) and fences
// duplicate or old updates. The read port exposes the pre-edge state.
module quote_store_engine #(
    parameter integer INSTRUMENTS = 4,
    parameter integer VENUES = 4,
    parameter integer PRICE_W = 20,
    parameter integer QTY_W = 14,
    parameter integer TIME_W = 16,
    parameter integer SEQ_W = 12,
    parameter integer TAG_W = 16,
    parameter integer INST_W = INSTRUMENTS > 1 ? $clog2(INSTRUMENTS + 1) : 1,
    parameter integer VENUE_W = VENUES > 1 ? $clog2(VENUES + 1) : 1,
    localparam integer RECORD_W = 2 * (PRICE_W + QTY_W) + TIME_W + SEQ_W
)(
    input  wire                      clk,
    input  wire                      rst,
    input  wire  [TIME_W-1:0]        now_tick,
    input  wire                      cmd_valid,
    output wire                      cmd_ready,
    input  wire  [2:0]               cmd_op,
    input  wire  [INST_W-1:0]        cmd_instrument,
    input  wire  [VENUE_W-1:0]       cmd_venue,
    input  wire  [SEQ_W-1:0]         cmd_sequence,
    input  wire  [PRICE_W-1:0]       cmd_bid_price,
    input  wire  [PRICE_W-1:0]       cmd_ask_price,
    input  wire  [QTY_W-1:0]         cmd_bid_qty,
    input  wire  [QTY_W-1:0]         cmd_ask_qty,
    input  wire  [TAG_W-1:0]         cmd_tag,
    output logic                     ack_valid,
    input  wire                      ack_ready,
    output logic [3:0]               ack_code,
    output logic [TAG_W-1:0]         ack_tag,
    input  wire  [INST_W-1:0]        read_instrument,
    output wire  [VENUES-1:0]        read_enabled,
    output wire  [VENUES-1:0]        read_present,
    output wire  [VENUES*RECORD_W-1:0] read_records,
    output wire                      mutation_commit
);
    logic [VENUES-1:0] enabled [0:INSTRUMENTS-1];
    logic [VENUES-1:0] present [0:INSTRUMENTS-1];
    logic [VENUES-1:0] seen_sequence [0:INSTRUMENTS-1];
    logic [SEQ_W-1:0] sequence_bank [0:INSTRUMENTS-1][0:VENUES-1];
    logic [RECORD_W-SEQ_W-1:0] quote_bank [0:INSTRUMENTS-1][0:VENUES-1];
    localparam integer ROW_W = INSTRUMENTS > 1 ? $clog2(INSTRUMENTS) : 1;
    localparam integer COL_W = VENUES > 1 ? $clog2(VENUES) : 1;

    wire write_inst_valid = int'(cmd_instrument) < INSTRUMENTS;
    wire write_venue_valid = int'(cmd_venue) < VENUES;
    wire [ROW_W-1:0] row = INSTRUMENTS == 1 ? '0 : ROW_W'(cmd_instrument);
    wire [COL_W-1:0] column = VENUES == 1 ? '0 : COL_W'(cmd_venue);
    wire [ROW_W-1:0] query_row = INSTRUMENTS == 1 ? '0 : ROW_W'(read_instrument);
    wire read_valid = int'(read_instrument) < INSTRUMENTS;
    wire [SEQ_W-1:0] last_sequence = sequence_bank[row][column];
    wire [SEQ_W-1:0] seq_delta = cmd_sequence - last_sequence;
    wire bid_shape_ok = (cmd_bid_qty == 0) == (cmd_bid_price == 0);
    wire ask_shape_ok = (cmd_ask_qty == 0) == (cmd_ask_price == 0);
    wire is_two_sided = cmd_bid_qty != 0 && cmd_ask_qty != 0;
    wire quote_shape_ok = bid_shape_ok && ask_shape_ok &&
        (cmd_bid_qty != 0 || cmd_ask_qty != 0) &&
        (!is_two_sided || cmd_bid_price <= cmd_ask_price);

    logic [3:0] command_code;
    // Idempotent venue-control writes leave an existing quote generation
    // intact. Only a real market or enable-state change resets availability.
    wire changes_row = cmd_op == `QUOTE_REPLACE || cmd_op == `QUOTE_WITHDRAW ||
                       (cmd_op == `QUOTE_ENABLE && !enabled[row][column]) ||
                       (cmd_op == `QUOTE_DISABLE && enabled[row][column]);
    assign mutation_commit = cmd_valid && cmd_ready && command_code == `QUOTE_OK && changes_row;
    assign cmd_ready = !rst && (!ack_valid || ack_ready);
    assign read_enabled = read_valid ? enabled[query_row] : '0;
    assign read_present = read_valid ? present[query_row] : '0;

    for (genvar venue = 0; venue < VENUES; venue++) begin : g_read
        assign read_records[venue*RECORD_W +: RECORD_W] =
            read_valid && present[query_row][venue] ?
            {sequence_bank[query_row][venue], quote_bank[query_row][venue]} : '0;
    end

    // Error priority is deterministic. A malformed update can be retried with
    // the same sequence because only successful market-data operations commit it.
    always @* begin
        command_code = `QUOTE_OK;
        if (!write_inst_valid)
            command_code = `QUOTE_BAD_INST;
        else if (!write_venue_valid)
            command_code = `QUOTE_BAD_VENUE;
        else begin
            case (cmd_op)
                `QUOTE_REPLACE, `QUOTE_WITHDRAW: begin
                    if (!enabled[row][column])
                        command_code = `QUOTE_DISABLED;
                    else if (seen_sequence[row][column] &&
                             (seq_delta == 0 || seq_delta[SEQ_W-1]))
                        command_code = `QUOTE_BAD_SEQ;
                    else if (cmd_op == `QUOTE_REPLACE && !quote_shape_ok)
                        command_code = `QUOTE_BAD_PRICE;
                end
                `QUOTE_ENABLE, `QUOTE_DISABLE: command_code = `QUOTE_OK;
                default: command_code = `QUOTE_BAD_OP;
            endcase
        end
    end

    // Acknowledgement and state mutation share the accepting edge. When the
    // acknowledgement stalls, the store cannot consume another command.
    always_ff @(posedge clk) begin
        if (rst) begin
            ack_valid <= 1'b0;
            ack_code <= '0;
            ack_tag <= '0;
            for (integer instrument = 0; instrument < INSTRUMENTS; instrument++) begin
                enabled[instrument] <= '1;
                present[instrument] <= '0;
                seen_sequence[instrument] <= '0;
            end
        end else if (cmd_ready) begin
            ack_valid <= cmd_valid;
            if (cmd_valid) begin
                ack_code <= command_code;
                ack_tag <= cmd_tag;
                if (command_code == `QUOTE_OK) begin
                    case (cmd_op)
                        `QUOTE_REPLACE: begin
                            quote_bank[row][column] <= {
                                now_tick, cmd_bid_qty, cmd_ask_qty,
                                cmd_bid_price, cmd_ask_price
                            };
                            sequence_bank[row][column] <= cmd_sequence;
                            seen_sequence[row][column] <= 1'b1;
                            present[row][column] <= 1'b1;
                        end
                        `QUOTE_WITHDRAW: begin
                            sequence_bank[row][column] <= cmd_sequence;
                            seen_sequence[row][column] <= 1'b1;
                            present[row][column] <= 1'b0;
                        end
                        `QUOTE_DISABLE: begin
                            enabled[row][column] <= 1'b0;
                            present[row][column] <= 1'b0;
                        end
                        `QUOTE_ENABLE: enabled[row][column] <= 1'b1;
                        default: ;
                    endcase
                end
            end
        end
    end
endmodule
