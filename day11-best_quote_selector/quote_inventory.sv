`timescale 1ns/1ps
`default_nettype none

// Consumed liquidity belongs to the exact store mutation generation. A quote
// update replenishes inventory and advances that generation without wrapping.
module quote_inventory #(
    parameter integer INSTRUMENTS = 4,
    parameter integer VENUES = 4,
    parameter integer QTY_W = 14,
    parameter integer REV_W = 32,
    localparam integer INST_W = $clog2(INSTRUMENTS+1),
    localparam integer VENUE_W = $clog2(VENUES+1)
) (
    input wire clk, rst,
    input wire mutation,
    input wire [INST_W-1:0] mutation_instrument,
    input wire [VENUE_W-1:0] mutation_venue,
    input wire dispatch,
    input wire [INST_W-1:0] dispatch_instrument,
    input wire dispatch_buy,
    input wire [VENUES*QTY_W-1:0] dispatch_amounts,
    input wire [INST_W-1:0] read_instrument,
    input wire read_buy,
    output wire [VENUES*QTY_W-1:0] consumed,
    output wire [VENUES*REV_W-1:0] revisions,
    output wire [VENUES-1:0] poisoned
);
    localparam integer ROW_W = INSTRUMENTS > 1 ? $clog2(INSTRUMENTS) : 1;
    localparam logic [REV_W-1:0] LAST_REVISION = '1;
    wire [ROW_W-1:0] read_row = INSTRUMENTS == 1 ? '0 : ROW_W'(read_instrument);
    wire read_ok = int'(read_instrument) < INSTRUMENTS;
    wire [QTY_W-1:0] used [0:INSTRUMENTS-1][0:VENUES-1][0:1];
    wire [REV_W-1:0] revision [0:INSTRUMENTS-1][0:VENUES-1];
    wire exhausted [0:INSTRUMENTS-1][0:VENUES-1];

    for (genvar venue = 0; venue < VENUES; venue++) begin : g_read
        assign consumed[venue*QTY_W +: QTY_W] = read_ok ? used[read_row][venue][read_buy] : '0;
        assign revisions[venue*REV_W +: REV_W] = read_ok ? revision[read_row][venue] : '0;
        assign poisoned[venue] = !read_ok || exhausted[read_row][venue];
    end

    for (genvar row = 0; row < INSTRUMENTS; row++) begin : g_book
        for (genvar venue = 0; venue < VENUES; venue++) begin : g_venue
            logic [REV_W-1:0] generation;
            logic poison;
            logic [QTY_W-1:0] bid, ask;
            wire [QTY_W-1:0] amount = dispatch_amounts[venue*QTY_W +: QTY_W];
            wire replace_row = mutation && mutation_instrument == INST_W'(row) &&
                               mutation_venue == VENUE_W'(venue);
            wire debit = dispatch && dispatch_instrument == INST_W'(row) && amount != 0;
            assign revision[row][venue] = generation;
            assign exhausted[row][venue] = poison;
            assign used[row][venue][0] = bid;
            assign used[row][venue][1] = ask;

            always_ff @(posedge clk) begin
                if (rst) begin
                    generation <= '0;
                    poison <= 1'b0;
                    bid <= '0;
                    ask <= '0;
                end else if (replace_row) begin
                    if (generation == LAST_REVISION)
                        poison <= 1'b1;
                    else
                        generation <= generation + 1'b1;
                    bid <= '0;
                    ask <= '0;
                end else if (debit) begin
                    if (dispatch_buy)
                        ask <= ask + amount;
                    else
                        bid <= bid + amount;
                end
            end
        end
    end
endmodule

`default_nettype wire
