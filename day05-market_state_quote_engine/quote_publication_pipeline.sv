`timescale 1ns/1ps
`default_nettype none

module quote_publication_pipeline #(
    parameter integer BANKS = 2,
    parameter integer PRICE_W = 5,
    parameter integer QTY_W = 12,
    parameter integer EPOCH_W = 8,
    parameter integer REV_W = 16,
    localparam integer BANK_W = $clog2(BANKS+1),
    localparam integer KEY_W = 1+EPOCH_W+2*PRICE_W+QTY_W
) (
    input wire clk,
    input wire rst,
    input wire [BANKS*KEY_W-1:0] desired_now,
    input wire [BANKS*3-1:0] reason_now,
    output wire q_valid,
    input wire q_ready,
    output wire [BANK_W-1:0] q_instrument,
    output wire q_active,
    output wire [EPOCH_W-1:0] q_epoch,
    output wire [REV_W-1:0] q_revision,
    output wire [PRICE_W-1:0] q_bid,
    output wire [PRICE_W-1:0] q_ask,
    output wire [QTY_W-1:0] q_size,
    output wire [2:0] q_reason,
    output wire q_current
);
    logic [BANKS*KEY_W-1:0] desired_stage;
    logic [BANKS*3-1:0] reason_stage;
    wire [KEY_W-1:0] offered_key = {q_active, q_epoch, q_bid, q_ask, q_size};

    // The stage separates price/imbalance arithmetic from the arbitration
    // and revision-counter feedback path. It samples continuously.
    always_ff @(posedge clk) begin
        if (rst) begin
            desired_stage <= '0;
            reason_stage <= '0;
        end else begin
            desired_stage <= desired_now;
            reason_stage <= reason_now;
        end
    end

    quote_publication #(
        .BANKS(BANKS), .PRICE_W(PRICE_W), .QTY_W(QTY_W), .EPOCH_W(EPOCH_W), .REV_W(REV_W)
    ) scheduler (
        .clk(clk), .rst(rst), .desired(desired_stage), .desired_reason(reason_stage),
        .q_valid(q_valid), .q_ready(q_ready), .q_instrument(q_instrument), .q_active(q_active),
        .q_epoch(q_epoch), .q_revision(q_revision), .q_bid(q_bid), .q_ask(q_ask), .q_size(q_size),
        .q_reason(q_reason)
    );

    // Live applicability bypasses the data pipeline and never changes the
    // accepted quote's price, size, revision, or reason.
    assign q_current = q_valid &&
        desired_now[int'(q_instrument)*KEY_W+:KEY_W] == offered_key;
endmodule

`default_nettype wire
