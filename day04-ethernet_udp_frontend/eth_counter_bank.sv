`timescale 1ns/1ps

module eth_counter_bank #(
    parameter integer ENTRIES = 32,
    parameter integer COUNTER_BITS = 64
) (
    input  logic                  clk,
    input  logic                  rst,
    input  logic [16*ENTRIES-1:0] deltas,
    input  logic                  clear_live,
    input  logic                  capture,
    input  logic [4:0]            read_index,
    output logic [63:0]           read_value,
    output logic [31:0]           snapshot_id
);
    logic [COUNTER_BITS-1:0] live [0:ENTRIES-1];
    logic [COUNTER_BITS-1:0] frozen [0:ENTRIES-1];

    assign read_value = 64'(frozen[read_index]);

    for (genvar i = 0; i < ENTRIES; i = i+1) begin : g_counter
        logic [COUNTER_BITS:0] next_count;
        logic [15:0] increment;

        assign increment = deltas[16*i +: 16];
        assign next_count = {1'b0,live[i]} + (COUNTER_BITS+1)'(increment);

        always_ff @(posedge clk) begin
            if (rst) begin
                live[i]   <= '0;
                frozen[i] <= '0;
            end else begin
                if (clear_live)
                    live[i] <= '0;
                else if (increment != 0)
                    live[i] <= next_count[COUNTER_BITS] ? {COUNTER_BITS{1'b1}} : next_count[COUNTER_BITS-1:0];

                // When clear and capture coincide, keep the preceding interval.
                if (capture)
                    frozen[i] <= live[i];
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst)
            snapshot_id <= '0;
        else if (capture)
            snapshot_id <= snapshot_id + 32'd1;
    end
endmodule
