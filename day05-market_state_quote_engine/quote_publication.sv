`timescale 1ns/1ps
`default_nettype none

module quote_publication #(
    parameter integer BANKS = 2,
    parameter integer PRICE_W = 5,
    parameter integer QTY_W = 12,
    parameter integer EPOCH_W = 8,
    parameter integer REV_W = 16,
    localparam integer BANK_W = $clog2(BANKS + 1),
    localparam integer KEY_W = 1+EPOCH_W+2*PRICE_W+QTY_W
) (
    input wire clk,
    input wire rst,
    input wire [BANKS*KEY_W-1:0] desired,
    input wire [BANKS*3-1:0] desired_reason,
    output wire q_valid,
    input wire q_ready,
    output logic [BANK_W-1:0] q_instrument,
    output wire q_active,
    output wire [EPOCH_W-1:0] q_epoch,
    output logic [REV_W-1:0] q_revision,
    output wire [PRICE_W-1:0] q_bid,
    output wire [PRICE_W-1:0] q_ask,
    output wire [QTY_W-1:0] q_size,
    output logic [2:0] q_reason
);
    localparam integer INDEX_W = (BANKS > 1) ? $clog2(BANKS) : 1;
    logic [KEY_W-1:0] last_key [0:BANKS-1];
    logic [REV_W-1:0] revision [0:BANKS-1];
    logic [INDEX_W-1:0] cursor;
    logic occupied;
    logic [KEY_W-1:0] output_key;
    wire [BANKS-1:0] dirty;
    logic [INDEX_W-1:0] selected;
    logic found;

    for (genvar bank = 0; bank < BANKS; bank++) begin : compare
        wire [KEY_W-1:0] current = desired[bank*KEY_W+:KEY_W];
        wire active_now = current[KEY_W-1];
        wire sent_active = last_key[bank][KEY_W-1];
        assign dirty[bank] = (active_now || sent_active) && current != last_key[bank];
    end

    assign q_valid = occupied && !rst;
    assign {q_active, q_epoch, q_bid, q_ask, q_size} = output_key;

    always_comb begin
        integer bank_index;
        selected = cursor;
        found = 1'b0;
        for (integer offset = 0; offset < BANKS; offset++) begin
            bank_index = int'(cursor) + offset;
            if (bank_index >= BANKS)
                bank_index = bank_index - BANKS;
            if (!found && dirty[bank_index]) begin
                selected = INDEX_W'(bank_index);
                found = 1'b1;
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            cursor <= '0;
            occupied <= 1'b0;
            q_instrument <= '0;
            output_key <= '0;
            q_reason <= '0;
            q_revision <= '0;
            for (integer bank = 0; bank < BANKS; bank++) begin
                last_key[bank] <= '0;
                revision[bank] <= '0;
            end
        end else if (!occupied || q_ready) begin
            occupied <= found;
            if (found) begin
                q_instrument <= BANK_W'(selected);
                output_key <= desired[int'(selected)*KEY_W+:KEY_W];
                q_reason <= desired_reason[int'(selected)*3+:3];
                q_revision <= revision[selected];
                last_key[selected] <= desired[int'(selected)*KEY_W+:KEY_W];
                revision[selected] <= revision[selected] + REV_W'(1);
                cursor <= (selected == INDEX_W'(BANKS-1)) ? INDEX_W'(0) : selected + INDEX_W'(1);
            end
        end
    end
endmodule

`default_nettype wire
