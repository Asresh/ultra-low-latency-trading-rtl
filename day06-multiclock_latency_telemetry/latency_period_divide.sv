`timescale 1ns/1ps
`default_nettype none

// The PPS feedback path has time for bit-serial division. Compute the
// nearest 32.32 nanosecond period from an integer reference interval.
module latency_period_divide (
    input wire clk, rst,
    input wire s_valid,
    output wire s_ready,
    input wire [63:0] s_reference_ns, s_ticks,
    output wire m_valid,
    input wire m_ready,
    output logic [63:0] m_period,
    output logic m_overflow, m_zero_ticks
);
    localparam integer DIV_BITS = 96;
    localparam integer COUNT_W = $clog2(DIV_BITS+1);
    logic [COUNT_W-1:0] remaining;
    logic [DIV_BITS-1:0] numerator;
    logic [DIV_BITS-2:0] quotient;
    logic [63:0] divisor, remainder;
    logic pending;

    wire [64:0] shifted = {remainder,numerator[DIV_BITS-1]};
    wire subtract = shifted >= {1'b0,divisor};
    wire [63:0] remainder_next = subtract ? 64'(shifted - {1'b0,divisor}) : shifted[63:0];
    wire [DIV_BITS-1:0] quotient_next = {quotient,subtract};
    wire [64:0] double_remainder = {remainder_next,1'b0};
    wire round_up = double_remainder > {1'b0,divisor} ||
                    (double_remainder == {1'b0,divisor} && quotient_next[0]);
    wire [DIV_BITS:0] rounded = {1'b0,quotient_next} + (DIV_BITS+1)'(round_up);

    assign s_ready = !rst && remaining == 0 && (!pending || m_ready);
    assign m_valid = !rst && pending;

    always_ff @(posedge clk) begin
        if (rst) begin
            remaining <= '0;
            numerator <= '0;
            quotient <= '0;
            divisor <= '0;
            remainder <= '0;
            pending <= 0;
            m_period <= '0;
            m_overflow <= 0;
            m_zero_ticks <= 0;
        end else begin
            if (m_ready)
                pending <= 0;
            if (s_valid && s_ready) begin
                if (s_ticks == 0) begin
                    pending <= 1;
                    m_period <= '0;
                    m_overflow <= 0;
                    m_zero_ticks <= 1;
                end else begin
                    numerator <= {s_reference_ns,32'd0};
                    quotient <= '0;
                    divisor <= s_ticks;
                    remainder <= '0;
                    remaining <= COUNT_W'(DIV_BITS);
                end
            end else if (remaining != 0) begin
                numerator <= {numerator[DIV_BITS-2:0],1'b0};
                quotient <= quotient_next[DIV_BITS-2:0];
                remainder <= remainder_next;
                remaining <= remaining - 1'b1;
                if (remaining == 1) begin
                    pending <= 1;
                    m_period <= (|rounded[DIV_BITS:64]) ? '0 : rounded[63:0];
                    m_overflow <= |rounded[DIV_BITS:64];
                    m_zero_ticks <= 0;
                end
            end
        end
    end
endmodule

`default_nettype wire
