`timescale 1ns/1ps
`default_nettype none

module market_best_price #(
    parameter integer LEVELS = 16,
    parameter bit HIGH = 1'b0,
    localparam integer PRICE_W = $clog2(LEVELS + 1)
) (
    input wire [LEVELS-1:0] occupied,
    output wire valid,
    output wire [PRICE_W-1:0] price
);
    localparam integer LEAVES = 1 << $clog2(LEVELS);
    wire [2*LEAVES-1:1] has_level;
    wire [PRICE_W-1:0] level_index [1:2*LEAVES-1];

    for (genvar i = 0; i < LEAVES; i++) begin : leaf
        if (i < LEVELS) begin : used
            assign has_level[LEAVES+i] = occupied[i];
        end else begin : padding
            assign has_level[LEAVES+i] = 1'b0;
        end
        assign level_index[LEAVES+i] = PRICE_W'(i);
    end

    for (genvar i = 1; i < LEAVES; i++) begin : node
        localparam integer PRIMARY = 2*i + (HIGH ? 1 : 0);
        localparam integer SECONDARY = 2*i + (HIGH ? 0 : 1);
        assign has_level[i] = has_level[PRIMARY] || has_level[SECONDARY];
        assign level_index[i] = has_level[PRIMARY] ? level_index[PRIMARY] : level_index[SECONDARY];
    end

    assign valid = has_level[1];
    assign price = valid ? level_index[1] : '0;
endmodule

`default_nettype wire
