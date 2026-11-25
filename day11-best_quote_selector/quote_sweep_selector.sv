`timescale 1ns/1ps
`default_nettype none

// Independent sweep top sharing the same normalized command and quote store
// as the BBO top. Policy commits and commands take effect after their edge.
module quote_sweep_selector #(
    parameter integer INSTRUMENTS = 4,
    parameter integer VENUES = 4,
    parameter integer PRICE_W = 20,
    parameter integer QTY_W = 14,
    parameter integer TIME_W = 16,
    parameter integer SEQ_W = 12,
    parameter integer TAG_W = 16,
    parameter integer FEE_W = 12,
    parameter integer EPOCH_W = 16,
    localparam integer INST_W = $clog2(INSTRUMENTS+1),
    localparam integer VENUE_W = $clog2(VENUES+1),
    localparam integer REQUEST_W = QTY_W + $clog2(VENUES),
    localparam integer UNIT_W = (PRICE_W+1 > FEE_W ? PRICE_W+1 : FEE_W) + 1,
    localparam integer VALUE_W = UNIT_W + REQUEST_W,
    localparam integer RECORD_W = 2*(PRICE_W+QTY_W) + TIME_W + SEQ_W,
    localparam integer CHILD_W = VENUE_W + PRICE_W + QTY_W + FEE_W + TIME_W + SEQ_W
) (
    input wire clk, rst,
    input wire [TIME_W-1:0] now_tick,

    input wire cmd_valid,
    output wire cmd_ready,
    input wire [2:0] cmd_op,
    input wire [INST_W-1:0] cmd_instrument,
    input wire [VENUE_W-1:0] cmd_venue,
    input wire [SEQ_W-1:0] cmd_sequence,
    input wire [PRICE_W-1:0] cmd_bid_price, cmd_ask_price,
    input wire [QTY_W-1:0] cmd_bid_qty, cmd_ask_qty,
    input wire [TAG_W-1:0] cmd_tag,
    output wire ack_valid,
    input wire ack_ready,
    output wire [3:0] ack_code,
    output wire [TAG_W-1:0] ack_tag,

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

    input wire query_valid,
    output wire query_ready,
    input wire [INST_W-1:0] query_instrument,
    input wire [TAG_W-1:0] query_tag,
    input wire query_buy,
    input wire [REQUEST_W-1:0] query_quantity,
    input wire [PRICE_W-1:0] query_limit,
    input wire [TIME_W-1:0] query_max_age,
    input wire [VENUES-1:0] query_venues,
    input wire query_all_or_none,

    output wire result_valid,
    input wire result_ready,
    output wire [3:0] result_status,
    output wire [TAG_W-1:0] result_tag,
    output wire [TIME_W-1:0] result_tick,
    output wire [EPOCH_W-1:0] result_epoch,
    output wire [VENUES-1:0] result_eligible, result_stale,
    output wire [VENUE_W-1:0] result_count,
    output wire [REQUEST_W-1:0] result_quantity,
    output wire signed [VALUE_W-1:0] result_value,
    output wire [VENUES*CHILD_W-1:0] result_children
);
    wire [VENUES-1:0] read_enabled, read_present;
    wire [VENUES*RECORD_W-1:0] read_records;
    localparam integer ROW_W = VENUES > 1 ? $clog2(VENUES) : 1;
    logic active_bank;
    logic [VENUES-1:0] enabled [0:1];
    logic [VENUES*QTY_W-1:0] caps [0:1];
    logic [VENUES*FEE_W-1:0] fees [0:1];
    wire [ROW_W-1:0] write_row = VENUES == 1 ? '0 : ROW_W'(cfg_venue);

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

    quote_store #(
        .INSTRUMENTS(INSTRUMENTS), .VENUES(VENUES), .PRICE_W(PRICE_W),
        .QTY_W(QTY_W), .TIME_W(TIME_W), .SEQ_W(SEQ_W), .TAG_W(TAG_W)
    ) store (
        .read_instrument(query_instrument), .*
    );

    quote_sweep_plan #(
        .INSTRUMENTS(INSTRUMENTS), .VENUES(VENUES), .PRICE_W(PRICE_W),
        .QTY_W(QTY_W), .TIME_W(TIME_W), .SEQ_W(SEQ_W), .TAG_W(TAG_W),
        .FEE_W(FEE_W), .EPOCH_W(EPOCH_W)
    ) plan (
        .policy_enabled(enabled[active_bank]),
        .policy_caps(caps[active_bank]),
        .policy_fees(fees[active_bank]),
        .*
    );
endmodule

`default_nettype wire
