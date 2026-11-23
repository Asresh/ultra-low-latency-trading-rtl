`timescale 1ns/1ps
`default_nettype none

module matching_fifo #(
    parameter integer WIDTH = 32,
    parameter integer DEPTH = 4,
    localparam integer PTR_W = (DEPTH > 1) ? $clog2(DEPTH) : 1,
    localparam integer CNT_W = $clog2(DEPTH + 1)
) (
    input wire clk,
    input wire rst,
    input wire s_valid,
    output wire s_ready,
    input wire [WIDTH-1:0] s_payload,
    output wire m_valid,
    input wire m_ready,
    output wire [WIDTH-1:0] m_payload
);
    logic [WIDTH-1:0] storage [0:DEPTH-1];
    logic [PTR_W-1:0] read_pointer, write_pointer;
    logic [CNT_W-1:0] used;

    assign s_ready = !rst && (used < CNT_W'(DEPTH));
    assign m_valid = !rst && (used != '0);
    assign m_payload = storage[read_pointer];
    wire push = s_valid && s_ready;
    wire pop = m_valid && m_ready;

    function automatic [PTR_W-1:0] advance(input [PTR_W-1:0] ptr);
        advance = (ptr == PTR_W'(DEPTH-1)) ? PTR_W'(0) : ptr + PTR_W'(1);
    endfunction

    always_ff @(posedge clk) begin
        if (rst) begin
            read_pointer <= '0;
            write_pointer <= '0;
            used <= '0;
        end else begin
            if (push) begin
                storage[write_pointer] <= s_payload;
                write_pointer <= advance(write_pointer);
            end
            if (pop)
                read_pointer <= advance(read_pointer);
            case ({push, pop})
                2'b10: used <= used + CNT_W'(1);
                2'b01: used <= used - CNT_W'(1);
                default: ;
            endcase
        end
    end
endmodule

`default_nettype wire
