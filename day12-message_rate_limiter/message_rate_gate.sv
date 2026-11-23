`timescale 1ns/1ps
`default_nettype none

// Join policy decisions to opaque message data and split the result into an
// audit stream and a forwarded-message stream. Every accepted command needs
// an audit transfer. Only a PASS also needs a data transfer.
module message_rate_gate #(
    parameter integer KEYS = 5,
    parameter integer KEY_W = (KEYS < 2) ? 1 : $clog2(KEYS),
    parameter integer TICK_W = 12,
    parameter integer TOKEN_W = 12,
    parameter integer COUNT_W = 16,
    parameter integer TAG_W = 16,
    parameter integer DATA_W = 32
) (
    input logic clk,
    input logic rst,
    input logic s_valid,
    output logic s_ready,
    input logic [1:0] s_op,
    input logic [KEY_W-1:0] s_key,
    input logic [TICK_W-1:0] s_tick,
    input logic [TOKEN_W-1:0] s_amount,
    input logic [TOKEN_W-1:0] s_rate,
    input logic s_enable,
    input logic [TAG_W-1:0] s_tag,
    input logic [DATA_W-1:0] s_data,

    output logic audit_valid,
    input logic audit_ready,
    output logic [1:0] audit_op,
    output logic [KEY_W-1:0] audit_key,
    output logic [TAG_W-1:0] audit_tag,
    output logic [3:0] audit_status,
    output logic [TOKEN_W-1:0] audit_balance,
    output logic [COUNT_W-1:0] audit_passed,
    output logic [COUNT_W-1:0] audit_limited,
    output logic [COUNT_W-1:0] audit_charged,
    output logic [2:0] audit_saturated,

    output logic out_valid,
    input logic out_ready,
    output logic [KEY_W-1:0] out_key,
    output logic [TAG_W-1:0] out_tag,
    output logic [DATA_W-1:0] out_data
);
    localparam logic [3:0] PASS = 4'd1;
    logic result_valid;
    logic result_ready;
    logic audit_done;
    logic output_done;
    logic data_required;
    logic [DATA_W-1:0] saved_data;
    logic unused_enabled;
    logic [TICK_W-1:0] unused_anchor;
    logic [TOKEN_W-1:0] unused_capacity;
    logic [TOKEN_W-1:0] unused_rate;
    logic [TOKEN_W-1:0] unused_before;

    assign data_required = audit_status == PASS;
    assign audit_valid = result_valid && !audit_done && !rst;
    assign out_valid = result_valid && data_required && !output_done && !rst;
    // Both acknowledgements can arrive on the same edge or in either order.
    assign result_ready = (audit_done || audit_ready)
                        && (!data_required || output_done || out_ready);
    assign out_key = audit_key;
    assign out_tag = audit_tag;
    assign out_data = saved_data;

    message_rate_limiter #(
        .KEYS(KEYS),
        .KEY_W(KEY_W),
        .TICK_W(TICK_W),
        .TOKEN_W(TOKEN_W),
        .COUNT_W(COUNT_W),
        .TAG_W(TAG_W)
    ) policy (
        .clk(clk),
        .rst(rst),
        .s_valid(s_valid),
        .s_ready(s_ready),
        .s_op(s_op),
        .s_key(s_key),
        .s_tick(s_tick),
        .s_amount(s_amount),
        .s_rate(s_rate),
        .s_enable(s_enable),
        .s_tag(s_tag),
        .m_valid(result_valid),
        .m_ready(result_ready),
        .m_op(audit_op),
        .m_key(audit_key),
        .m_tag(audit_tag),
        .m_status(audit_status),
        .m_enabled(unused_enabled),
        .m_anchor(unused_anchor),
        .m_capacity(unused_capacity),
        .m_rate(unused_rate),
        .m_before(unused_before),
        .m_balance(audit_balance),
        .m_passed(audit_passed),
        .m_limited(audit_limited),
        .m_charged(audit_charged),
        .m_saturated(audit_saturated)
    );

    always_ff @(posedge clk) begin
        if (rst) begin
            saved_data <= '0;
            audit_done <= 1'b0;
            output_done <= 1'b0;
        end else begin
            if (s_valid && s_ready)
                saved_data <= s_data;
            if (result_valid && result_ready) begin
                // The acknowledged entry may be replaced by a new one now.
                audit_done <= 1'b0;
                output_done <= 1'b0;
            end else begin
                if (audit_valid && audit_ready)
                    audit_done <= 1'b1;
                if (out_valid && out_ready)
                    output_done <= 1'b1;
            end
        end
    end
endmodule

`default_nettype wire
