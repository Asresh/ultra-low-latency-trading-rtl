`timescale 1ns/1ps
`default_nettype none

module latency_probe #(
    parameter integer TAG_W = 16,
    parameter integer CLASS_W = 2,
    parameter integer PROBE_W = 2,
    parameter integer PROBE_NUMBER = 0,
    parameter integer EPOCH_W = 8,
    parameter integer FIFO_ADDRESS_W = 3,
    localparam integer RECORD_W = 65 + TAG_W + CLASS_W + PROBE_W + EPOCH_W
) (
    input  wire clk,
    input  wire reset,
    input  wire manager_clk,
    input  wire manager_reset,
    input  wire [63:0] gray_ticks_async,
    input  wire [EPOCH_W-1:0] epoch_gray_async,
    input  wire valid,
    output wire ready,
    input  wire [TAG_W-1:0] tag,
    input  wire [CLASS_W-1:0] event_class,
    input  wire end_event,
    output wire manager_valid,
    input  wire manager_ready,
    output wire [RECORD_W-1:0] observation
);
    (* ASYNC_REG = "TRUE" *) logic [63:0] gray_q1, gray_q2;
    (* ASYNC_REG = "TRUE" *) logic [EPOCH_W-1:0] epoch_q1, epoch_q2;
    logic [63:0] local_tick;
    logic time_live;

    // Reconstruct the binary time counter by accumulating Gray-code parity.
    function automatic logic [63:0] unpack_gray(input logic [63:0] value);
        logic [63:0] binary;
        integer bit_index;
        begin
            binary[63] = value[63];
            for (bit_index = 62; bit_index >= 0; bit_index = bit_index - 1)
                binary[bit_index] = binary[bit_index + 1] ^ value[bit_index];
            unpack_gray = binary;
        end
    endfunction

    always_ff @(posedge clk or posedge reset) begin
        if (reset) begin
            gray_q1 <= '0;
            gray_q2 <= '0;
            epoch_q1 <= '0;
            epoch_q2 <= '0;
            local_tick <= '0;
            time_live <= 1'b0;
        end else begin
            gray_q1 <= gray_ticks_async;
            gray_q2 <= gray_q1;
            epoch_q1 <= epoch_gray_async;
            epoch_q2 <= epoch_q1;
            local_tick <= unpack_gray(gray_q2);
            if (gray_q2 != '0)
                time_live <= 1'b1;
        end
    end

    wire fifo_ready;
    assign ready = time_live && fifo_ready;
    latency_async_fifo #(.WIDTH(RECORD_W), .ADDRESS_W(FIFO_ADDRESS_W)) crossing (
        .wclk(clk),
        .wreset(reset),
        .s_valid(valid && time_live),
        .s_ready(fifo_ready),
        .s_data({epoch_q2, end_event, event_class, tag, PROBE_W'(PROBE_NUMBER), local_tick}),
        .rclk(manager_clk),
        .rreset(manager_reset),
        .m_valid(manager_valid),
        .m_ready(manager_ready),
        .m_data(observation)
    );
endmodule

`default_nettype wire
