`timescale 1ns/1ps
`default_nettype none

// Debit against the pre-edge balance, then credit one cycle of refill.
// Reuse the widened, saturating arithmetic with a fixed one-cycle delta.
module eager_rate_bucket #(
    parameter integer TOKEN_W = 12
) (
    input wire clk,
    input wire rst,
    input wire configure,
    input wire new_enable,
    input wire [TOKEN_W-1:0] new_capacity,
    input wire [TOKEN_W-1:0] new_rate,
    input wire [TOKEN_W-1:0] new_reserve,
    input wire [TOKEN_W-1:0] debit,
    output logic enabled,
    output logic [TOKEN_W-1:0] capacity,
    output logic [TOKEN_W-1:0] reserve,
    output logic [TOKEN_W-1:0] tokens
);
    logic [TOKEN_W-1:0] rate;
    wire unused_time_order;
    wire [TOKEN_W-1:0] refilled;

    rate_refill #(.TICK_W(2),.TOKEN_W(TOKEN_W)) arithmetic (
        .tick(2'd1), .anchor(2'd0), .balance(tokens-debit), .capacity, .rate,
        .chronological(unused_time_order), .available(refilled)
    );

    always_ff @(posedge clk) begin
        if (rst) begin
            enabled <= 0;
            capacity <= '0;
            reserve <= '0;
            rate <= '0;
            tokens <= '0;
        end else if (configure) begin
            enabled <= new_enable;
            capacity <= new_capacity;
            reserve <= new_reserve;
            rate <= new_rate;
            tokens <= new_capacity;
        end else if (enabled) begin
            tokens <= refilled;
        end
    end
endmodule

`default_nettype wire
