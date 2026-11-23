`timescale 1ns/1ps
`default_nettype none

// Store compact, prevalidated events. The wrapper never writes during a run.
// Clearing a tape invalidates its length, so unused words need no reset.
module replay_event_store #(
    parameter integer DEPTH = 16,
    parameter integer INDEX_W = (DEPTH > 1) ? $clog2(DEPTH) : 1,
    parameter integer WORD_W = 121
) (
    input  wire              clk,
    input  wire              write_enable,
    input  wire [INDEX_W-1:0] write_index,
    input  wire [WORD_W-1:0]  write_word,
    input  wire [INDEX_W-1:0] read_index,
    output wire [WORD_W-1:0]  read_word
);
    logic [WORD_W-1:0] image [0:DEPTH-1];

    always_ff @(posedge clk)
        if (write_enable)
            image[write_index] <= write_word;

    assign read_word = image[read_index];
endmodule

`default_nettype wire
