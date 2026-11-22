`timescale 1ns/1ps
`default_nettype none

// The tournament chooses solely by occupancy, carrying the winning level
// and FIFO head in parallel. Each parent knows which child has priority.
module price_priority #(
    parameter integer LEVELS = 16,
    parameter integer HEAD_W = 5,
    parameter bit HIGH_FIRST = 1'b1,
    localparam integer PRICE_W = $clog2(LEVELS + 1)
) (
    input  wire [LEVELS-1:0] occupied,
    input  wire [LEVELS*HEAD_W-1:0] heads,
    output wire               valid,
    output wire [PRICE_W-1:0] price,
    output wire [HEAD_W-1:0] head
);
    localparam integer LEAVES = 1 << $clog2(LEVELS);
    wire [2*LEAVES-1:1] node_valid;
    wire [PRICE_W-1:0] node_price [1:2*LEAVES-1];
    wire [HEAD_W-1:0] node_head [1:2*LEAVES-1];

    for (genvar leaf = 0; leaf < LEAVES; leaf++) begin : gen_leaf
        if (leaf < LEVELS) begin : gen_live
            assign node_valid[LEAVES+leaf] = occupied[leaf];
            assign node_head[LEAVES+leaf] = heads[leaf*HEAD_W+:HEAD_W];
        end else begin : gen_padding
            assign node_valid[LEAVES+leaf] = 1'b0;
            assign node_head[LEAVES+leaf] = '0;
        end
        assign node_price[LEAVES+leaf] = PRICE_W'(leaf);
    end

    for (genvar node = 1; node < LEAVES; node++) begin : gen_parent
        localparam integer PRIORITY = 2*node + (HIGH_FIRST ? 1 : 0);
        localparam integer FALLBACK = 2*node + (HIGH_FIRST ? 0 : 1);
        assign node_valid[node] = node_valid[PRIORITY] | node_valid[FALLBACK];
        assign node_price[node] = node_valid[PRIORITY]
                                ? node_price[PRIORITY] : node_price[FALLBACK];
        assign node_head[node] = node_valid[PRIORITY]
                               ? node_head[PRIORITY] : node_head[FALLBACK];
    end

    assign valid = node_valid[1];
    assign price = valid ? node_price[1] : '0;
    // The consumer uses the head only when valid. Empty queues need not
    // store initialized pointers.
    assign head = node_head[1];
endmodule

`default_nettype wire
