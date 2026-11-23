`timescale 1ns/1ps

module itch_segmenter #(
    parameter integer MESSAGE_BYTES = 64
) (
    input  logic                        clk,
    input  logic                        clear,
    input  logic                        s_valid,
    output logic                        s_ready,
    input  logic [63:0]                 s_sequence,
    input  logic [15:0]                 s_length,
    input  logic [MESSAGE_BYTES*8-1:0] s_payload,
    output logic                        m_valid,
    input  logic                        m_ready,
    output logic [63:0]                 m_data,
    output logic [7:0]                  m_keep,
    output logic                        m_first,
    output logic                        m_last,
    output logic [63:0]                 m_sequence
);
    logic occupied, first_word, consume, accept;
    logic [15:0] remaining;
    logic [MESSAGE_BYTES*8-1:0] payload;

    assign m_valid = occupied && !clear;
    assign m_data = payload[63:0];
    assign m_first = first_word;
    assign m_last = remaining <= 16'd8;
    assign consume = m_valid && m_ready;
    assign s_ready = !clear && (!occupied || (consume && m_last));
    assign accept = s_valid && s_ready;

    for (genvar lane = 0; lane < 8; lane = lane + 1) begin : g_keep
        assign m_keep[lane] = remaining > 16'(lane);
    end

    always_ff @(posedge clk) begin
        if (clear) begin
            occupied   <= 1'b0;
            first_word <= 1'b0;
            remaining  <= '0;
            m_sequence <= '0;
        end else if (accept) begin
            occupied   <= 1'b1;
            first_word <= 1'b1;
            remaining  <= s_length;
            payload    <= s_payload;
            m_sequence <= s_sequence;
        end else if (consume) begin
            if (m_last)
                occupied <= 1'b0;
            else begin
                first_word <= 1'b0;
                remaining  <= remaining - 16'd8;
                payload    <= payload >> 64;
            end
        end
    end
endmodule
