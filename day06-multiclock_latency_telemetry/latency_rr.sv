`timescale 1ns/1ps
`default_nettype none

module latency_rr #(
    parameter integer PORTS = 3,
    parameter integer WIDTH = 84,
    localparam integer INDEX_W = (PORTS > 1) ? $clog2(PORTS) : 1
) (
    input  wire                   clk,
    input  wire                   reset,
    input  wire [PORTS-1:0]       s_valid,
    output logic [PORTS-1:0]      s_ready,
    input  wire [PORTS*WIDTH-1:0] s_data,
    output logic                 m_valid,
    input  wire                   m_ready,
    output logic [WIDTH-1:0]     m_data
);
    logic [INDEX_W-1:0] cursor;
    logic held;
    logic [INDEX_W-1:0] held_port;
    logic [INDEX_W-1:0] chosen;

    always_comb begin : find_candidate
        integer index;
        logic found;
        chosen = held_port;
        found = held && s_valid[held_port];
        for (integer offset = 0; offset < PORTS; offset = offset + 1) begin
            index = int'(cursor) + offset;
            if (index >= PORTS)
                index = index - PORTS;
            if (!held && !found && s_valid[index]) begin
                chosen = INDEX_W'(index);
                found = 1'b1;
            end
        end
        m_valid = !reset && found;
        m_data = found ? s_data[int'(chosen)*WIDTH +: WIDTH] : '0;
        s_ready = '0;
        if (m_valid && m_ready)
            s_ready[chosen] = 1'b1;
    end

    always_ff @(posedge clk or posedge reset) begin
        if (reset) begin
            cursor <= '0;
            held <= 1'b0;
            held_port <= '0;
        end else if (m_valid) begin
            if (m_ready) begin
                held <= 1'b0;
                cursor <= chosen == INDEX_W'(PORTS - 1) ? '0 : chosen + 1'b1;
            end else begin
                held <= 1'b1;
                held_port <= chosen;
            end
        end
    end
endmodule

`default_nettype wire
