`timescale 1ns/1ps
`default_nettype none

// Calculate the balance a bucket would have at the request timestamp.
// Half of the tick space distinguishes forward time from old or ambiguous time.
module rate_refill #(
    parameter integer TICK_W = 12,
    parameter integer TOKEN_W = 12
) (
    input  logic [TICK_W-1:0] tick,
    input  logic [TICK_W-1:0] anchor,
    input  logic [TOKEN_W-1:0] balance,
    input  logic [TOKEN_W-1:0] capacity,
    input  logic [TOKEN_W-1:0] rate,
    output logic              chronological,
    output logic [TOKEN_W-1:0] available
);
    localparam integer PRODUCT_W = TICK_W + TOKEN_W;
    logic [TICK_W-1:0] elapsed;
    logic [PRODUCT_W-1:0] refill;
    logic [PRODUCT_W:0] total;

    always_comb begin
        elapsed = tick - anchor;
        chronological = !elapsed[TICK_W-1];
        refill = elapsed * rate;
        total = {1'b0, refill} + {{(TICK_W+1){1'b0}}, balance};
        if (!chronological)
            available = balance;
        else if (total >= {{(TICK_W+1){1'b0}}, capacity})
            available = capacity;
        else
            available = total[TOKEN_W-1:0];
    end
endmodule

`default_nettype wire
