`timescale 1ns/1ps
`default_nettype none

// Exact bounded event evidence for one order generation. Contents are only
// retired by a successful new reservation in this slot.
module risk_venue_history #(
    parameter integer DEPTH = 8,
    parameter integer SEQ_W = 16,
    parameter integer PRICE_W = 24,
    parameter integer QTY_W = 16,
    parameter integer MATCH_W = 48,
    parameter integer ACTION_W = 8,
    localparam integer WORD_W = 3 + SEQ_W + PRICE_W + QTY_W + MATCH_W + ACTION_W,
    localparam integer PTR_W = DEPTH > 1 ? $clog2(DEPTH) : 1
) (
    input wire clk, rst,
    input wire replace_generation,
    input wire append,
    input wire [2:0] kind,
    input wire [SEQ_W-1:0] sequence_number,
    input wire [PRICE_W-1:0] price,
    input wire [QTY_W-1:0] quantity,
    input wire [MATCH_W-1:0] match_id,
    input wire [ACTION_W-1:0] action_id,
    output logic sequence_seen,
    output logic exact_replay,
    output logic execution_seen
);
    logic [PTR_W-1:0] next_row;
    logic [DEPTH-1:0] valid;
    logic [WORD_W-1:0] history [0:DEPTH-1];
    localparam integer SEQ_LOW = PRICE_W + QTY_W + MATCH_W + ACTION_W;
    wire [WORD_W-1:0] offered = {kind, sequence_number, price, quantity, action_id, match_id};

    always @* begin
        sequence_seen = 1'b0;
        exact_replay = 1'b0;
        execution_seen = 1'b0;
        for (integer row = 0; row < DEPTH; row++) begin
            if (valid[row]) begin
                if (history[row][SEQ_LOW +: SEQ_W] == sequence_number) begin
                    sequence_seen = 1'b1;
                    exact_replay |= history[row] == offered;
                end
                execution_seen |= match_id != 0 && history[row][MATCH_W-1:0] == match_id;
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst || replace_generation) begin
            next_row <= '0;
            valid <= '0;
        end else if (append) begin
            valid[next_row] <= 1'b1;
            history[next_row] <= offered;
            next_row <= next_row == PTR_W'(DEPTH-1) ? '0 : next_row + 1'b1;
        end
    end
endmodule

`default_nettype wire
