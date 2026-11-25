`timescale 1ns/1ps
`default_nettype none

// One retained result decouples the consumer from slot invalidation upstream.
module mold_session_mux #(
    parameter integer SLOTS = 3,
    parameter integer WIDTH = 16,
    localparam integer SLOT_W = SLOTS > 1 ? $clog2(SLOTS) : 1
) (
    input  wire                   clk,
    input  wire                   rst,
    input  wire [SLOTS-1:0]       s_valid,
    output logic [SLOTS-1:0]      s_ready,
    input  wire [SLOTS*WIDTH-1:0] s_data,
    output logic                  m_valid,
    input  wire                   m_ready,
    output logic [SLOT_W-1:0]     m_slot,
    output logic [WIDTH-1:0]      m_data
);
    logic [SLOT_W-1:0] cursor, chosen;
    logic found;

    always_comb begin
        integer index;
        chosen = cursor;
        found = 1'b0;
        s_ready = '0;
        for (integer offset = 0; offset < SLOTS; offset++) begin
            index = int'(cursor) + offset;
            if (index >= SLOTS)
                index = index - SLOTS;
            if (!found && s_valid[index]) begin
                found = 1'b1;
                chosen = SLOT_W'(index);
            end
        end
        if (!rst && found && (!m_valid || m_ready))
            s_ready[chosen] = 1'b1;
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            cursor <= '0;
            m_slot <= '0;
            m_data <= '0;
            m_valid <= 1'b0;
        end else if (!m_valid || m_ready) begin
            m_valid <= found;
            if (found) begin
                m_slot <= chosen;
                m_data <= s_data[int'(chosen)*WIDTH+:WIDTH];
                cursor <= chosen == SLOT_W'(SLOTS-1) ? '0 : chosen + 1'b1;
            end
        end
    end
endmodule

`default_nettype wire
