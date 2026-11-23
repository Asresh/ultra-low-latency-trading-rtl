`timescale 1ns/1ps
`default_nettype none
`include "notional_codes.svh"

// Calculate a rounded order amount and fee before evaluating its cash limit.
module fixed_point_notional #(
    parameter integer PRICE_FRAC = 8
) (
    input  wire         clk,
    input  wire         rst,
    input  wire         req_valid,
    output wire         req_ready,
    input  wire [15:0]  req_tag,
    input  wire [31:0]  req_price,
    input  wire [31:0]  req_qty,
    input  wire [15:0]  req_fee_bps,
    input  wire [1:0]   req_round,
    input  wire         req_sell,
    input  wire         req_limit_en,
    input  wire [63:0]  req_limit,
    output wire         rsp_valid,
    input  wire         rsp_ready,
    output logic [15:0] rsp_tag,
    output logic [2:0]  rsp_status,
    output logic        rsp_sell,
    output logic [63:0] rsp_gross,
    output logic [63:0] rsp_fee,
    output logic [63:0] rsp_cash,
    output logic        rsp_inexact
);
    localparam logic [2:0] IDLE = 0, MULTIPLY = 1, PRICE_ROUND = 2,
                           FEE_START = 3, FEE_WAIT = 4, REPLY = 5;
    localparam logic [63:0] SCALE = 64'd1 << PRICE_FRAC;
    localparam integer FEE_SCALE = 10000;

    logic [2:0] state, input_status;
    logic [31:0] price, quantity;
    logic [15:0] fee_bps;
    logic [1:0] mode;
    logic use_limit;
    logic [63:0] limit, product;
    logic gross_inexact;

    // Decide whether to increment a nonnegative truncated quotient.
    function automatic logic round_up(
        input logic [1:0] method,
        input logic quotient_odd,
        input logic [63:0] remainder,
        input logic [63:0] denominator
    );
        logic [64:0] double_remainder;
        begin
            double_remainder = {remainder,1'b0};
            case (method)
                2'd1: round_up = remainder != 0;
                2'd2: round_up = double_remainder > {1'b0,denominator} ||
                                   (double_remainder == {1'b0,denominator} && quotient_odd);
                default: round_up = 0;
            endcase
        end
    endfunction

    wire [63:0] gross_floor = product >> PRICE_FRAC;
    wire [63:0] gross_remainder = product & (SCALE - 1'b1);
    wire [63:0] gross_rounded =
        gross_floor + 64'(round_up(mode, gross_floor[0], gross_remainder, SCALE));
    wire [79:0] fee_numerator = rsp_gross * fee_bps;
    wire fee_input_ready, fee_result_valid;
    wire [79:0] fee_result;
    wire [13:0] fee_remainder;
    wire [63:0] fee_rounded =
        64'(fee_result) + 64'(round_up(mode, fee_result[0], 64'(fee_remainder), 64'(FEE_SCALE)));
    wire [64:0] cost =
        rsp_sell ? {1'b0,rsp_gross} - {1'b0,fee_rounded}
                 : {1'b0,rsp_gross} + {1'b0,fee_rounded};
    wire violates_limit = use_limit && (rsp_sell ? cost[63:0] < limit : cost[63:0] > limit);
    wire fee_outside_gross = |fee_result[79:64];

    assign req_ready = !rst && state == IDLE;
    assign rsp_valid = !rst && state == REPLY;

    notional_divide #(.NUM_W(80), .DIVISOR(FEE_SCALE)) fee_divider (
        .clk, .rst, .s_valid(state == FEE_START), .s_ready(fee_input_ready),
        .s_value(fee_numerator), .m_valid(fee_result_valid), .m_ready(state == FEE_WAIT),
        .m_quotient(fee_result), .m_remainder(fee_remainder)
    );

    always_comb begin
        input_status = `NOTIONAL_OK;
        if (req_qty == 0)
            input_status = `NOTIONAL_QTY;
        else if (req_price == 0)
            input_status = `NOTIONAL_PRICE;
        else if (req_fee_bps > 16'(FEE_SCALE))
            input_status = `NOTIONAL_FEE;
        else if (req_round == 3)
            input_status = `NOTIONAL_MODE;
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= IDLE;
            price <= 0;
            quantity <= 0;
            fee_bps <= 0;
            mode <= 0;
            use_limit <= 0;
            limit <= 0;
            product <= 0;
            gross_inexact <= 0;
            rsp_tag <= 0;
            rsp_sell <= 0;
            rsp_status <= `NOTIONAL_OK;
            rsp_gross <= 0;
            rsp_fee <= 0;
            rsp_cash <= 0;
            rsp_inexact <= 0;
        end else begin
            case (state)
                IDLE: if (req_valid && req_ready) begin
                    price <= req_price;
                    quantity <= req_qty;
                    fee_bps <= req_fee_bps;
                    mode <= req_round;
                    limit <= req_limit;
                    use_limit <= req_limit_en;
                    rsp_tag <= req_tag;
                    rsp_sell <= req_sell;
                    rsp_status <= input_status;
                    rsp_gross <= 0;
                    rsp_fee <= 0;
                    rsp_cash <= 0;
                    rsp_inexact <= 0;
                    state <= input_status == `NOTIONAL_OK ? MULTIPLY : REPLY;
                end
                MULTIPLY: begin
                    product <= price * quantity;
                    state <= PRICE_ROUND;
                end
                PRICE_ROUND: begin
                    rsp_gross <= gross_rounded;
                    gross_inexact <= gross_remainder != 0;
                    state <= FEE_START;
                end
                FEE_START: if (fee_input_ready)
                    state <= FEE_WAIT;
                FEE_WAIT: if (fee_result_valid) begin
                    rsp_fee <= fee_rounded;
                    rsp_inexact <= gross_inexact || fee_remainder != 0;
                    if (cost[64] || fee_outside_gross) begin
                        rsp_cash <= '1;
                        rsp_status <= `NOTIONAL_OVERFLOW;
                    end else begin
                        rsp_cash <= cost[63:0];
                        rsp_status <= violates_limit ? `NOTIONAL_LIMIT : `NOTIONAL_OK;
                    end
                    state <= REPLY;
                end
                REPLY: if (rsp_ready)
                    state <= IDLE;
                default: state <= IDLE;
            endcase
        end
    end
endmodule

`default_nettype wire
