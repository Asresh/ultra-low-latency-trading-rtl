`timescale 1ns/1ps

module mold_statistics #(
    parameter integer COUNTERS = 16
) (
    input  logic                     clk,
    input  logic                     clear,
    input  logic [COUNTERS-1:0]     increment,
    input  logic [5:0]               occupancy,
    input  logic                     snapshot,
    input  logic [3:0]               read_index,
    output logic [31:0]              read_data,
    output logic [5:0]               peak_occupancy
);
    logic [31:0] live_counts [0:COUNTERS-1];
    logic [31:0] held_counts [0:COUNTERS-1];
    logic [5:0] held_peak;

    assign read_data = held_counts[read_index];
    assign peak_occupancy = held_peak;

    for (genvar index = 0; index < COUNTERS; index = index + 1) begin : g_counter
        always_ff @(posedge clk) begin
            if (clear) begin
                live_counts[index] <= 32'd0;
                held_counts[index] <= 32'd0;
            end else begin
                if (increment[index] && !(&live_counts[index]))
                    live_counts[index] <= live_counts[index] + 32'd1;
                if (snapshot)
                    held_counts[index] <= live_counts[index];
            end
        end
    end

    logic [5:0] live_peak;
    always_ff @(posedge clk) begin
        if (clear) begin
            live_peak <= '0;
            held_peak <= '0;
        end else begin
            if (occupancy > live_peak)
                live_peak <= occupancy;
            if (snapshot)
                held_peak <= live_peak;
        end
    end
endmodule
