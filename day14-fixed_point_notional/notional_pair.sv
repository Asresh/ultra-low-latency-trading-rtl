`timescale 1ns/1ps
`default_nettype none

// Two small calculator lanes share an ordered completion interface.
// A two-entry queue keeps the selected lane for each admitted request.
module notional_pair #(
    parameter integer PRICE_FRAC = 8
) (
    input  wire          clk,
    input  wire          rst,
    input  wire          s_valid,
    output wire          s_ready,
    input  wire [163:0]  s_request,
    output wire          m_valid,
    input  wire          m_ready,
    output wire [212:0]  m_reply,
    output wire [1:0]    outstanding
);
    logic preference;
    logic [1:0] queue;
    logic write_slot, read_slot;
    logic [1:0] count;
    wire [1:0] lane_ready, lane_valid;
    wire [212:0] lane_response [0:1];
    wire [15:0] tag, fee_bps;
    wire [31:0] price, qty;
    wire [1:0] rounding;
    wire sell, limit_en;
    wire [63:0] limit_value;

    assign {tag,price,qty,fee_bps,rounding,sell,limit_en,limit_value} = s_request;
    wire lane = lane_ready[preference] ? preference : !preference;
    wire next_lane = queue[read_slot];
    wire send = s_valid && s_ready;
    wire receive = m_valid && m_ready;

    assign s_ready = !rst && count < 2 && |lane_ready;
    assign m_valid = !rst && count != 0 && lane_valid[next_lane];
    assign m_reply = m_valid ? lane_response[next_lane] : '0;
    assign outstanding = count;

    for (genvar i = 0; i < 2; i++) begin : calculator
        wire [15:0] result_tag;
        wire [2:0] result_code;
        wire result_side, result_inexact;
        wire [63:0] gross, fee, cash;

        fixed_point_notional #(.PRICE_FRAC(PRICE_FRAC)) core (
            .clk, .rst, .req_valid(send && lane == 1'(i)), .req_ready(lane_ready[i]),
            .req_tag(tag), .req_price(price), .req_qty(qty), .req_fee_bps(fee_bps),
            .req_round(rounding), .req_sell(sell), .req_limit_en(limit_en), .req_limit(limit_value),
            .rsp_valid(lane_valid[i]), .rsp_ready(receive && next_lane == 1'(i)),
            .rsp_tag(result_tag), .rsp_status(result_code), .rsp_sell(result_side),
            .rsp_gross(gross), .rsp_fee(fee), .rsp_cash(cash), .rsp_inexact(result_inexact)
        );
        assign lane_response[i] = {result_tag,result_code,result_side,gross,fee,cash,result_inexact};
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            preference <= 0;
            queue <= '0;
            write_slot <= 0;
            read_slot <= 0;
            count <= 0;
        end else begin
            if (send) begin
                preference <= !lane;
                queue[write_slot] <= lane;
                write_slot <= !write_slot;
            end
            if (receive)
                read_slot <= !read_slot;
            if (send != receive)
                count <= send ? count + 1'b1 : count - 1'b1;
        end
    end
endmodule

`default_nettype wire
