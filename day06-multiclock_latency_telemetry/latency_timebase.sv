`timescale 1ns/1ps
`default_nettype none

// Free-running raw ticks are the only multibit clock value distributed to
// probes. Nanoseconds are a separate local 64.32 accumulator.
module latency_timebase #(
    parameter logic [31:0] INITIAL_NS = 32'd8,
    parameter logic [31:0] INITIAL_FRACTION = 32'd0
) (
    input  wire         clk,
    input  wire         reset,
    input  wire         pps_async,
    input  wire         rate_valid,
    output wire         rate_ready,
    input  wire [31:0]  rate_ns,
    input  wire [31:0]  rate_fraction,
    output logic [63:0] ticks,
    output logic [63:0] gray_ticks,
    output wire [63:0]  nanoseconds,
    output wire [31:0]  subnanoseconds,
    output logic        saturated,
    output logic        invalid_rate,
    output logic        pps_valid,
    input  wire         pps_ready,
    output logic [63:0] pps_ticks,
    output logic [63:0] pps_nanoseconds,
    output logic [31:0] pps_missed
);
    logic [95:0] time_q;
    logic [63:0] period_q;
    (* ASYNC_REG = "TRUE" *) logic [1:0] pps_sync;
    logic pps_old;

    wire [63:0] next_tick = ticks + 1'b1;
    wire [96:0] elapsed = {1'b0, time_q} + {33'b0, period_q};
    wire pps_edge = pps_sync[1] && !pps_old;
    wire pps_room = !pps_valid || pps_ready;

    assign rate_ready = !reset;
    assign nanoseconds = time_q[95:32];
    assign subnanoseconds = time_q[31:0];

    always_ff @(posedge clk or posedge reset) begin
        if (reset) begin
            ticks <= '0;
            gray_ticks <= '0;
            time_q <= '0;
            period_q <= {INITIAL_NS, INITIAL_FRACTION};
            saturated <= 1'b0;
            invalid_rate <= 1'b0;
            pps_sync <= '0;
            pps_old <= 1'b0;
            pps_valid <= 1'b0;
            pps_ticks <= '0;
            pps_nanoseconds <= '0;
            pps_missed <= '0;
        end else begin
            pps_sync <= {pps_sync[0], pps_async};
            pps_old <= pps_sync[1];
            ticks <= next_tick;
            gray_ticks <= next_tick ^ (next_tick >> 1);
            if (elapsed[96]) begin
                time_q <= '1;
                saturated <= 1'b1;
            end else if (!saturated) begin
                time_q <= elapsed[95:0];
            end
            if (rate_valid && rate_ready) begin
                if ({rate_ns, rate_fraction} == '0)
                    invalid_rate <= 1'b1;
                else
                    period_q <= {rate_ns, rate_fraction};
            end
            if (pps_valid && pps_ready)
                pps_valid <= 1'b0;
            if (pps_edge) begin
                if (pps_room) begin
                    pps_valid <= 1'b1;
                    pps_ticks <= ticks;
                    pps_nanoseconds <= nanoseconds;
                end else if (pps_missed != '1) begin
                    pps_missed <= pps_missed + 1'b1;
                end
            end
        end
    end
endmodule

`default_nettype wire
