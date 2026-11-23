`timescale 1ns/1ps
`default_nettype none

module matching_arbiter #(
    parameter integer PORTS = 4,
    parameter integer WIDTH = 32,
    localparam integer PORT_W = (PORTS > 1) ? $clog2(PORTS) : 1
) (
    input wire clk,
    input wire rst,
    input wire [PORTS-1:0] s_valid,
    output logic [PORTS-1:0] s_ready,
    input wire [PORTS*WIDTH-1:0] s_payload,
    output logic m_valid,
    input wire m_ready,
    output logic [WIDTH-1:0] m_payload
);
    logic [PORT_W-1:0] next_first, held_port;
    logic held;
    logic [PORT_W-1:0] selected;
    logic picked;

    always_comb begin
        integer candidate;
        selected = held_port;
        picked = held && s_valid[held_port];
        for (integer offset = 0; offset < PORTS; offset++) begin
            candidate = int'(next_first) + offset;
            if (candidate >= PORTS)
                candidate = candidate - PORTS;
            if (!held && !picked && s_valid[candidate]) begin
                selected = PORT_W'(candidate);
                picked = 1'b1;
            end
        end
        m_valid = !rst && picked;
        m_payload = picked ? s_payload[int'(selected)*WIDTH+:WIDTH] : '0;
        s_ready = '0;
        if (m_valid && m_ready)
            s_ready[selected] = 1'b1;
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            next_first <= '0;
            held_port <= '0;
            held <= 1'b0;
        end else if (m_valid) begin
            if (m_ready) begin
                held <= 1'b0;
                next_first <= (selected == PORT_W'(PORTS-1)) ? PORT_W'(0) : selected + PORT_W'(1);
            end else begin
                held <= 1'b1;
                held_port <= selected;
            end
        end
    end
endmodule

`default_nettype wire
