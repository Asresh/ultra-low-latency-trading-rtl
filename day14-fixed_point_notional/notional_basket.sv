`timescale 1ns/1ps
`default_nettype none
`include "notional_codes.svh"

// Check the cash requirement of a bounded basket of buy and sell legs.
module notional_basket #(
    parameter integer MAX_LEGS = 8,
    parameter integer PRICE_FRAC = 8,
    localparam integer COUNT_W = $clog2(MAX_LEGS+2),
    localparam integer ACC_W = 65 + $clog2(MAX_LEGS)
) (
    input  wire                    clk,
    input  wire                    rst,
    input  wire                    req_valid,
    output wire                    req_ready,
    input  wire [15:0]             req_tag,
    input  wire [COUNT_W-1:0]      req_count,
    input  wire                    req_debit_en,
    input  wire [63:0]             req_max_debit,
    input  wire                    req_credit_en,
    input  wire [63:0]             req_min_credit,

    input  wire                    leg_valid,
    output wire                    leg_ready,
    input  wire                    leg_last,
    input  wire [31:0]             leg_price,
    input  wire [31:0]             leg_qty,
    input  wire [15:0]             leg_fee_bps,
    input  wire [1:0]              leg_round,
    input  wire                    leg_sell,

    output wire                    rsp_valid,
    input  wire                    rsp_ready,
    output logic [15:0]            rsp_tag,
    output logic [2:0]             rsp_status,
    output logic [COUNT_W-1:0]     rsp_count,
    output logic [COUNT_W-1:0]     rsp_bad_leg,
    output logic [2:0]             rsp_leg_status,
    output logic [ACC_W-2:0]       rsp_gross,
    output logic [ACC_W-2:0]       rsp_fee,
    output logic signed [ACC_W-1:0] rsp_net_cash,
    output logic                   rsp_inexact
);
    localparam logic [2:0] IDLE = 0, LEGS = 1, WAIT = 2, DRAIN = 3,
                           CHECK = 4, REPLY = 5;
    localparam logic [2:0] OK = 0, BAD_COUNT = 1, BAD_LEG = 2, DEBIT = 3, CREDIT = 4;
    localparam logic [COUNT_W-1:0] CAPACITY = COUNT_W'(MAX_LEGS);
    localparam logic [COUNT_W-1:0] OVER_CAPACITY = COUNT_W'(MAX_LEGS+1);

    logic [2:0] state;
    logic [COUNT_W-1:0] declared;
    logic valid_header, final_leg, debit_on, credit_on;
    logic [63:0] max_debit, min_credit;
    wire cell_ready, cell_valid, cell_sell, cell_inexact;
    wire [2:0] cell_status;
    wire [63:0] cell_gross, cell_fee, cell_cash;
    wire [15:0] unused_cell_tag;
    wire evaluate_leg = state == LEGS && valid_header && rsp_count < declared;
    wire signed [ACC_W-1:0] cash_magnitude = $signed(ACC_W'(cell_cash));

    assign req_ready = !rst && state == IDLE;
    assign leg_ready = !rst && (state == DRAIN || (state == LEGS && (!evaluate_leg || cell_ready)));
    assign rsp_valid = !rst && state == REPLY;

    fixed_point_notional #(.PRICE_FRAC(PRICE_FRAC)) arithmetic (
        .clk, .rst, .req_valid(evaluate_leg && leg_valid), .req_ready(cell_ready),
        .req_tag(16'd0), .req_price(leg_price), .req_qty(leg_qty),
        .req_fee_bps(leg_fee_bps), .req_round(leg_round), .req_sell(leg_sell),
        .req_limit_en(1'b0), .req_limit(64'd0),
        .rsp_valid(cell_valid), .rsp_ready(state == WAIT), .rsp_tag(unused_cell_tag),
        .rsp_status(cell_status), .rsp_sell(cell_sell), .rsp_gross(cell_gross),
        .rsp_fee(cell_fee), .rsp_cash(cell_cash), .rsp_inexact(cell_inexact)
    );

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= IDLE;
            declared <= '0;
            valid_header <= 0;
            final_leg <= 0;
            debit_on <= 0;
            credit_on <= 0;
            max_debit <= '0;
            min_credit <= '0;
            rsp_tag <= '0;
            rsp_status <= OK;
            rsp_count <= '0;
            rsp_bad_leg <= '1;
            rsp_leg_status <= `NOTIONAL_OK;
            rsp_gross <= '0;
            rsp_fee <= '0;
            rsp_net_cash <= '0;
            rsp_inexact <= 0;
        end else begin
            case (state)
                IDLE: if (req_valid && req_ready) begin
                    rsp_tag <= req_tag;
                    rsp_status <= OK;
                    rsp_count <= '0;
                    rsp_bad_leg <= '1;
                    rsp_leg_status <= `NOTIONAL_OK;
                    rsp_gross <= '0;
                    rsp_fee <= '0;
                    rsp_net_cash <= '0;
                    rsp_inexact <= 0;
                    declared <= req_count;
                    valid_header <= req_count != 0 && req_count <= CAPACITY;
                    debit_on <= req_debit_en;
                    credit_on <= req_credit_en;
                    max_debit <= req_max_debit;
                    min_credit <= req_min_credit;
                    state <= LEGS;
                end
                LEGS, DRAIN: if (leg_valid && leg_ready) begin
                    if (rsp_count != OVER_CAPACITY)
                        rsp_count <= rsp_count + 1'b1;
                    if (evaluate_leg) begin
                        final_leg <= leg_last;
                        state <= WAIT;
                    end else begin
                        state <= leg_last ? CHECK : DRAIN;
                    end
                end
                WAIT: if (cell_valid) begin
                    if (cell_status == `NOTIONAL_OK) begin
                        rsp_gross <= rsp_gross + (ACC_W-1)'(cell_gross);
                        rsp_fee <= rsp_fee + (ACC_W-1)'(cell_fee);
                        rsp_net_cash <= rsp_net_cash + (cell_sell ? -cash_magnitude : cash_magnitude);
                        rsp_inexact <= rsp_inexact || cell_inexact;
                    end else if (rsp_leg_status == `NOTIONAL_OK) begin
                        rsp_bad_leg <= rsp_count - 1'b1;
                        rsp_leg_status <= cell_status;
                    end
                    state <= final_leg ? CHECK : LEGS;
                end
                CHECK: begin
                    if (!valid_header || rsp_count != declared)
                        rsp_status <= BAD_COUNT;
                    else if (rsp_leg_status != `NOTIONAL_OK)
                        rsp_status <= BAD_LEG;
                    else if (debit_on && rsp_net_cash > $signed(ACC_W'(max_debit)))
                        rsp_status <= DEBIT;
                    else if (credit_on && rsp_net_cash > -$signed(ACC_W'(min_credit)))
                        rsp_status <= CREDIT;
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
