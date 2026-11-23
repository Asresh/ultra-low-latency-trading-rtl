`timescale 1ns/1ps
`default_nettype none

module ouch_soup_tx (
    input wire clk,
    input wire flush,
    input wire s_valid,
    output wire s_ready,
    input ouch_wire_pkg::packet_t s_packet,
    output wire m_valid,
    input wire m_ready,
    output wire [63:0] m_data,
    output logic [7:0] m_keep,
    output wire m_last
);
    import ouch_wire_pkg::*;
    localparam integer REM_BITS = $clog2(MAX_BODY + 3);
    logic [BODY_BITS+15:0] shift_data;
    logic [REM_BITS-1:0] remaining;

    assign s_ready = !flush && remaining == 0;
    assign m_valid = !flush && remaining != 0;
    assign m_data = shift_data[63:0];
    assign m_last = m_valid && remaining <= 8;

    always @* begin
        for (integer i = 0; i < 8; i = i + 1)
            m_keep[i] = m_valid && (remaining > REM_BITS'(i));
    end

    always @(posedge clk) begin
        if (flush) begin
            remaining <= 0;
        end else if (s_valid && s_ready) begin
            shift_data <= {s_packet.body, 8'(s_packet.length), 8'h00};
            remaining <= REM_BITS'(s_packet.length) + 2;
        end else if (m_valid && m_ready) begin
            shift_data <= shift_data >> 64;
            if (m_last) remaining <= 0;
            else remaining <= remaining - 8;
        end
    end
endmodule

`default_nettype wire
