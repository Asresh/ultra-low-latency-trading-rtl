`timescale 1ns/1ps

module mold_rr_mux #(
    parameter integer WIDTH = 595
) (
    input  logic               clk,
    input  logic               clear,
    input  logic [3:0]         s_valid,
    output logic [3:0]         s_ready,
    input  logic [4*WIDTH-1:0] s_data,
    output logic               m_valid,
    input  logic               m_ready,
    output logic [WIDTH-1:0]   m_data,
    output logic [1:0]         m_index
);
    logic [1:0] next_priority, grant, search_index, held_index;
    logic locked, found;

    always @* begin
        found = 1'b0;
        grant = next_priority;
        for (integer offset = 0; offset < 4; offset = offset + 1) begin
            search_index = next_priority + 2'(offset);
            if (!found && s_valid[search_index]) begin
                found = 1'b1;
                grant = search_index;
            end
        end
    end

    assign m_index = locked ? held_index : grant;
    assign m_valid = !clear && s_valid[m_index];
    assign m_data = s_data[WIDTH*m_index +: WIDTH];
    assign s_ready = m_valid && m_ready ? 4'b0001 << m_index : 4'b0;

    always_ff @(posedge clk) begin
        if (clear) begin
            next_priority <= 2'd0;
            held_index    <= 2'd0;
            locked        <= 1'b0;
        end else if (m_valid) begin
            if (m_ready) begin
                next_priority <= m_index + 2'd1;
                locked        <= 1'b0;
            end else begin
                held_index <= m_index;
                locked     <= 1'b1;
            end
        end
    end
endmodule
