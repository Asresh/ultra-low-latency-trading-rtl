`timescale 1ns/1ps
`default_nettype none

// Per-venue fee and quantity policy shared by both sweep entry points.
module quote_fee_policy #(
    parameter integer VENUES = 4,
    parameter integer QTY_W = 14,
    parameter integer FEE_W = 12,
    parameter integer EPOCH_W = 16,
    localparam integer VENUE_W = $clog2(VENUES+1)
) (
    input wire clk, rst,
    input wire cfg_valid,
    output wire cfg_ready,
    input wire [VENUE_W-1:0] cfg_venue,
    input wire cfg_enabled,
    input wire [QTY_W-1:0] cfg_cap,
    input wire signed [FEE_W-1:0] cfg_fee,
    output logic cfg_error,
    input wire commit_valid,
    output wire commit_ready,
    output logic [EPOCH_W-1:0] policy_epoch,
    output wire [VENUES-1:0] policy_enabled,
    output wire [VENUES*QTY_W-1:0] policy_caps,
    output wire [VENUES*FEE_W-1:0] policy_fees
);
    localparam integer ROW_W = VENUES > 1 ? $clog2(VENUES) : 1;
    logic active_bank;
    logic [VENUES-1:0] enabled [0:1];
    logic [VENUES*QTY_W-1:0] caps [0:1];
    logic [VENUES*FEE_W-1:0] fees [0:1];
    wire [ROW_W-1:0] write_row = VENUES == 1 ? '0 : ROW_W'(cfg_venue);

    assign policy_enabled = enabled[active_bank];
    assign policy_caps = caps[active_bank];
    assign policy_fees = fees[active_bank];
    assign commit_ready = !rst;
    assign cfg_ready = !rst && !commit_valid;

    always_ff @(posedge clk) begin
        if (rst) begin
            active_bank <= 1'b0;
            policy_epoch <= '0;
            enabled[0] <= '1;
            enabled[1] <= '1;
            caps[0] <= '1;
            caps[1] <= '1;
            fees[0] <= '0;
            fees[1] <= '0;
            cfg_error <= 1'b0;
        end else begin
            cfg_error <= 1'b0;
            if (commit_valid) begin
                active_bank <= !active_bank;
                policy_epoch <= policy_epoch + 1'b1;
            end else if (cfg_valid) begin
                if (int'(cfg_venue) >= VENUES)
                    cfg_error <= 1'b1;
                else begin
                    enabled[!active_bank][write_row] <= cfg_enabled;
                    caps[!active_bank][write_row*QTY_W +: QTY_W] <= cfg_cap;
                    fees[!active_bank][write_row*FEE_W +: FEE_W] <= cfg_fee;
                end
            end
        end
    end

endmodule

`default_nettype wire
