`timescale 1ns/1ps

module eth_packet_arbiter #(
    parameter integer HIGH_BURST = 3,
    parameter integer WIDTH = 74
) (
    input  logic               clk,
    input  logic               rst,
    input  logic [1:0]         in_valid,
    output logic [1:0]         in_ready,
    input  logic [1:0]         in_last,
    input  logic [2*WIDTH-1:0] in_data,
    output logic               out_valid,
    input  logic               out_ready,
    output logic [WIDTH-1:0]   out_data,
    output logic               out_lane
);
    localparam integer BUDGET_BITS = $clog2(HIGH_BURST+1);
    localparam logic [BUDGET_BITS-1:0] MAX_BUDGET = BUDGET_BITS'(HIGH_BURST);

    logic [BUDGET_BITS-1:0] high_served;
    logic locked, held_lane, chosen, finish;

    // Lane 0 is the urgent lane. Its credit counts complete packets.
    assign chosen = in_valid[1] && (!in_valid[0] || high_served == MAX_BUDGET);
    assign out_lane = locked ? held_lane : chosen;
    assign out_data = in_data[WIDTH*out_lane +: WIDTH];
    assign out_valid = !rst && in_valid[out_lane];
    assign in_ready = out_valid && out_ready ? (2'b01 << out_lane) : 2'b00;
    assign finish = out_valid && out_ready && in_last[out_lane];

    always_ff @(posedge clk) begin
        if (rst) begin
            locked      <= 1'b0;
            held_lane   <= 1'b0;
            high_served <= '0;
        end else if (finish) begin
            locked <= 1'b0;
            if (out_lane)
                high_served <= '0;
            else if (high_served < MAX_BUDGET)
                high_served <= high_served + 1'b1;
        end else if (out_valid) begin
            locked    <= 1'b1;
            held_lane <= out_lane;
        end
    end
endmodule
