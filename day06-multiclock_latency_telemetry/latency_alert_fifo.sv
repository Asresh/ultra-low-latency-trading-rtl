`timescale 1ns/1ps
`default_nettype none

// A held alert is never overwritten. A same-edge pop frees one write slot.
module latency_alert_fifo #(
    parameter integer WIDTH = 282,
    parameter integer DEPTH = 8,
    localparam integer PTR_W = DEPTH > 1 ? $clog2(DEPTH) : 1,
    localparam integer USED_W = $clog2(DEPTH + 1)
) (
    input wire clk,
    input wire rst,
    input wire s_valid,
    output wire s_ready,
    input wire [WIDTH-1:0] s_data,
    output wire m_valid,
    input wire m_ready,
    output wire [WIDTH-1:0] m_data,
    output logic [USED_W-1:0] used
);
    logic [WIDTH-1:0] words [0:DEPTH-1];
    logic [PTR_W-1:0] head, tail;
    wire pop = m_valid && m_ready;
    wire push = s_valid && s_ready;
    assign m_valid = !rst && used != 0;
    assign s_ready = !rst && (used < USED_W'(DEPTH) || pop);
    assign m_data = m_valid ? words[head] : '0;

    function automatic logic [PTR_W-1:0] advance(input logic [PTR_W-1:0] pointer);
        advance = pointer == PTR_W'(DEPTH-1) ? '0 : pointer + 1'b1;
    endfunction

    always_ff @(posedge clk) begin
        if (rst) begin
            head <= '0;
            tail <= '0;
            used <= '0;
        end else begin
            if (push) begin
                words[tail] <= s_data;
                tail <= advance(tail);
            end
            if (pop)
                head <= advance(head);
            case ({push,pop})
                2'b10: used <= used + 1'b1;
                2'b01: used <= used - 1'b1;
                default: begin end
            endcase
        end
    end
endmodule

`default_nettype wire
