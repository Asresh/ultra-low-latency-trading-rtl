`timescale 1ns/1ps

module mold_feed #(
    parameter integer PACKET_BYTES = 1536,
    parameter integer MESSAGE_BYTES = 64,
    parameter integer BLOCKS = 64,
    parameter integer CANDIDATE_BITS = 83 + 8*MESSAGE_BYTES
) (
    input  logic                        clk,
    input  logic                        clear,
    input  logic                        session_active,
    input  logic [79:0]                 session_id,
    input  logic                        rx_valid,
    output logic                        rx_ready,
    input  logic [63:0]                 rx_data,
    input  logic [7:0]                  rx_keep,
    input  logic                        rx_first,
    input  logic                        rx_last,
    output logic [1:0]                  bank_valid,
    input  logic [1:0]                  bank_ready,
    output logic [2*CANDIDATE_BITS-1:0] bank_data
);
    logic [1:0] capture_ready, capture_valid;
    logic attached, selected, preferred, target_bank;

    assign target_bank = attached ? selected
                                  : capture_ready[preferred] ? preferred : !preferred;
    assign rx_ready = capture_ready[target_bank];
    assign capture_valid[0] = rx_valid && !target_bank;
    assign capture_valid[1] = rx_valid &&  target_bank;

    always_ff @(posedge clk) begin
        if (clear) begin
            attached  <= 1'b0;
            selected  <= 1'b0;
            preferred <= 1'b0;
        end else if (rx_valid && rx_ready) begin
            attached <= !rx_last;
            selected <= target_bank;
            if (rx_last)
                preferred <= !target_bank;
        end
    end

    for (genvar bank = 0; bank < 2; bank = bank + 1) begin : g_bank
        logic [2:0] kind;
        logic [63:0] seq;
        logic [15:0] length;
        logic [MESSAGE_BYTES*8-1:0] payload;

        mold_datagram_bank #(
            .PACKET_BYTES(PACKET_BYTES), .MESSAGE_BYTES(MESSAGE_BYTES), .BLOCKS(BLOCKS)
        ) capture (
            .clk, .clear, .session_active, .session_id,
            .rx_valid(capture_valid[bank]), .rx_ready(capture_ready[bank]),
            .rx_data, .rx_keep, .rx_first, .rx_last,
            .candidate_valid(bank_valid[bank]), .candidate_ready(bank_ready[bank]),
            .candidate_kind(kind), .candidate_seq(seq), .candidate_length(length),
            .candidate_payload(payload)
        );

        assign bank_data[bank*CANDIDATE_BITS +: CANDIDATE_BITS] = {kind, seq, length, payload};
    end
endmodule
