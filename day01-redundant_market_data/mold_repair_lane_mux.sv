`timescale 1ns/1ps
`default_nettype none

// Share a physical receiver lane between live traffic and admitted replay.
// Selection is retained from the first offered beat through the final transfer.
module mold_repair_lane_mux #(
    parameter integer KEY_W = 2
) (
    input wire clk, rst,
    input wire [1:0] s_valid,
    output wire [1:0] s_ready,
    input wire [127:0] s_data,
    input wire [15:0] s_keep,
    input wire [1:0] s_first, s_last,
    input wire [2*KEY_W-1:0] s_slot,
    output wire m_valid,
    input wire m_ready,
    output wire [63:0] m_data,
    output wire [7:0] m_keep,
    output wire m_first, m_last,
    output wire [KEY_W-1:0] m_slot
);
    logic locked, owner, preferred;
    wire selected = locked ? owner : (s_valid[preferred] ? preferred : !preferred);

    assign m_valid = !rst && s_valid[selected];
    assign m_data = m_valid ? s_data[64*selected+:64] : '0;
    assign m_keep = m_valid ? s_keep[8*selected+:8] : '0;
    assign m_first = m_valid && s_first[selected];
    assign m_last = m_valid && s_last[selected];
    assign m_slot = m_valid ? s_slot[KEY_W*selected+:KEY_W] : '0;
    assign s_ready[0] = !rst && !selected && m_ready;
    assign s_ready[1] = !rst && selected && m_ready;

    always_ff @(posedge clk) begin
        if (rst) begin
            locked <= 0;
            owner <= 0;
            preferred <= 0;
        end else if (m_valid) begin
            owner <= selected;
            locked <= !m_ready || !m_last;
            if (m_ready && m_last)
                preferred <= !selected;
        end
    end
endmodule

`default_nettype wire
