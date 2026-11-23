`timescale 1ns/1ps
`default_nettype none

// Unsigned restoring division with one quotient digit per clock.
module notional_divide #(
    parameter integer NUM_W = 80,
    parameter integer DIVISOR = 10000,
    localparam integer REM_W = $clog2(DIVISOR),
    localparam integer COUNT_W = $clog2(NUM_W + 1)
) (
    input  wire             clk,
    input  wire             rst,
    input  wire             s_valid,
    output wire             s_ready,
    input  wire [NUM_W-1:0] s_value,
    output wire             m_valid,
    input  wire             m_ready,
    output wire [NUM_W-1:0] m_quotient,
    output wire [REM_W-1:0] m_remainder
);
    logic [NUM_W-1:0] remaining_bits, quotient;
    logic [REM_W-1:0] remainder;
    logic [COUNT_W-1:0] digits;
    logic result_valid;

    wire [REM_W:0] trial = {remainder, remaining_bits[NUM_W-1]};
    wire one = trial >= (REM_W+1)'(DIVISOR);
    wire [REM_W-1:0] reduced = REM_W'(trial - ((REM_W+1)'(DIVISOR) & {(REM_W+1){one}}));

    assign s_ready = !rst && digits == 0 && (!result_valid || m_ready);
    assign m_valid = !rst && result_valid;
    assign m_quotient = quotient;
    assign m_remainder = remainder;

    always_ff @(posedge clk) begin
        if (rst) begin
            remaining_bits <= '0;
            quotient <= '0;
            remainder <= '0;
            digits <= '0;
            result_valid <= 0;
        end else begin
            if (m_valid && m_ready)
                result_valid <= 0;
            if (s_valid && s_ready) begin
                remaining_bits <= s_value;
                quotient <= '0;
                remainder <= '0;
                digits <= COUNT_W'(NUM_W);
            end else if (digits != 0) begin
                remaining_bits <= remaining_bits << 1;
                quotient <= (quotient << 1) | NUM_W'(one);
                remainder <= reduced;
                digits <= digits - 1'b1;
                if (digits == 1)
                    result_valid <= 1;
            end
        end
    end
endmodule

`default_nettype wire
