`timescale 1ns/1ps

module eth_tx_path #(
    parameter integer MAX_PAYLOAD = 1472,
    parameter integer HIGH_BURST = 3
) (
    input  logic         clk,
    input  logic         rst,
    input  logic [63:0]  timestamp,

    input  logic [1:0]   tx_desc_valid,
    output logic [1:0]   tx_desc_ready,
    input  logic [639:0] tx_desc_data,
    input  logic [1:0]   tx_payload_valid,
    output logic [1:0]   tx_payload_ready,
    input  logic [127:0] tx_payload_data,
    input  logic [15:0]  tx_payload_keep,
    input  logic [1:0]   tx_payload_first,
    input  logic [1:0]   tx_payload_last,

    output logic         tx_mac_valid,
    input  logic         tx_mac_ready,
    output logic [63:0]  tx_mac_data,
    output logic [7:0]   tx_mac_keep,
    output logic         tx_mac_first,
    output logic         tx_mac_last,
    output logic         tx_mac_lane,

    output logic         tx_status_valid,
    input  logic         tx_status_ready,
    output logic [127:0] tx_status_data
);
    logic [1:0] bank_data_valid, bank_data_ready, bank_last, bank_first;
    logic [147:0] bank_data;
    logic [1:0] bank_status_valid, bank_status_ready;
    logic [255:0] bank_status_data;
    logic [127:0] selected_status_with_unused_lane;
    logic status_lane;

    for (genvar index = 0; index < 2; index = index+1) begin : g_tx
        localparam integer STREAM_BASE = 74 * index;
        eth_tx_bank #(.MAX_PAYLOAD(MAX_PAYLOAD)) transmitter (
            .clk, .rst, .timestamp,
            .desc_valid(tx_desc_valid[index]), .desc_ready(tx_desc_ready[index]),
            .desc_data(tx_desc_data[320*index +: 320]),
            .s_valid(tx_payload_valid[index]), .s_ready(tx_payload_ready[index]),
            .s_data(tx_payload_data[64*index +: 64]), .s_keep(tx_payload_keep[8*index +: 8]),
            .s_first(tx_payload_first[index]), .s_last(tx_payload_last[index]),
            .m_valid(bank_data_valid[index]), .m_ready(bank_data_ready[index]),
            .m_data(bank_data[STREAM_BASE +: 64]), .m_keep(bank_data[STREAM_BASE+64 +: 8]),
            .m_first(bank_first[index]), .m_last(bank_last[index]),
            .done_valid(bank_status_valid[index]), .done_ready(bank_status_ready[index]),
            .done_data(bank_status_data[128*index +: 128])
        );
        assign bank_data[STREAM_BASE+72] = bank_first[index];
        assign bank_data[STREAM_BASE+73] = bank_last[index];
    end

    eth_packet_arbiter #(.HIGH_BURST(HIGH_BURST), .WIDTH(74)) frame_scheduler (
        .clk, .rst, .in_valid(bank_data_valid), .in_ready(bank_data_ready), .in_last(bank_last),
        .in_data(bank_data),
        .out_valid(tx_mac_valid), .out_ready(tx_mac_ready), .out_lane(tx_mac_lane),
        .out_data({tx_mac_last, tx_mac_first, tx_mac_keep, tx_mac_data})
    );

    eth_packet_arbiter #(.HIGH_BURST(1), .WIDTH(128)) completion_scheduler (
        .clk, .rst, .in_valid(bank_status_valid), .in_ready(bank_status_ready), .in_last(2'b11),
        .in_data(bank_status_data),
        .out_valid(tx_status_valid), .out_ready(tx_status_ready), .out_lane(status_lane),
        .out_data(selected_status_with_unused_lane)
    );

    assign tx_status_data = {selected_status_with_unused_lane[127:120], 7'd0, status_lane,
                             selected_status_with_unused_lane[111:0]};
endmodule
