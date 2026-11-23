`timescale 1ns/1ps
`include "frontend_codes.svh"

module ethernet_udp_frontend #(
    parameter integer MAX_FRAME = 1536,
    parameter integer MAX_PAYLOAD = 1472,
    parameter integer HIGH_BURST = 3,
    parameter bit ALLOW_ZERO_UDP = 1'b1
) (
    input  logic         clk,
    input  logic         rst,

    input  logic         rx_mac_valid,
    output logic         rx_mac_ready,
    input  logic [63:0]  rx_mac_data,
    input  logic [7:0]   rx_mac_keep,
    input  logic         rx_mac_first,
    input  logic         rx_mac_last,
    input  logic         rx_mac_bad,

    input  logic         rule_write_valid,
    output logic         rule_write_ready,
    input  logic [2:0]   rule_write_index,
    input  logic [255:0] rule_write_data,
    input  logic         rule_commit_valid,
    output logic         rule_commit_ready,
    output logic [7:0]   rule_epoch,

    output logic         rx_info_valid,
    input  logic         rx_info_ready,
    output logic [383:0] rx_info_data,

    output logic         rx_payload_valid,
    input  logic         rx_payload_ready,
    output logic [63:0]  rx_payload_data,
    output logic [7:0]   rx_payload_keep,
    output logic         rx_payload_first,
    output logic         rx_payload_last,

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
    output logic [127:0] tx_status_data,

    input  logic         stats_snapshot,
    input  logic         stats_clear_live,
    input  logic [4:0]   stats_index,
    output logic [63:0]  stats_data,
    output logic [31:0]  stats_snapshot_id
);
    `include "frontend_words.svh"

    logic [63:0] tick;
    logic [511:0] deltas;
    logic [7:0] rx_reason, tx_reason;
    logic rx_mac_fire, rx_info_fire, rx_body_fire, tx_mac_fire, tx_status_fire;
    logic [63:0] rx_latency;

    always_ff @(posedge clk)
        if (rst)
            tick <= 64'd0;
        else
            tick <= tick + 64'd1;

    eth_rx_path #(.MAX_FRAME(MAX_FRAME), .ALLOW_ZERO_UDP(ALLOW_ZERO_UDP)) receive_path (
        .clk, .rst, .timestamp(tick),
        .mac_valid(rx_mac_valid), .mac_ready(rx_mac_ready), .mac_data(rx_mac_data),
        .mac_keep(rx_mac_keep), .mac_first(rx_mac_first), .mac_last(rx_mac_last),
        .mac_bad(rx_mac_bad), .rule_write_valid, .rule_write_ready, .rule_write_index,
        .rule_write_data, .rule_commit_valid, .rule_commit_ready, .rule_epoch,
        .info_valid(rx_info_valid), .info_ready(rx_info_ready), .info_data(rx_info_data),
        .payload_valid(rx_payload_valid), .payload_ready(rx_payload_ready),
        .payload_data(rx_payload_data), .payload_keep(rx_payload_keep),
        .payload_first(rx_payload_first), .payload_last(rx_payload_last)
    );

    eth_tx_path #(.MAX_PAYLOAD(MAX_PAYLOAD), .HIGH_BURST(HIGH_BURST)) transmit_path (
        .clk, .rst, .timestamp(tick),
        .tx_desc_valid, .tx_desc_ready, .tx_desc_data,
        .tx_payload_valid, .tx_payload_ready, .tx_payload_data, .tx_payload_keep,
        .tx_payload_first, .tx_payload_last,
        .tx_mac_valid, .tx_mac_ready, .tx_mac_data, .tx_mac_keep,
        .tx_mac_first, .tx_mac_last, .tx_mac_lane,
        .tx_status_valid, .tx_status_ready, .tx_status_data
    );

    assign rx_mac_fire = rx_mac_valid && rx_mac_ready;
    assign rx_info_fire = rx_info_valid && rx_info_ready;
    assign rx_body_fire = rx_payload_valid && rx_payload_ready;
    assign tx_mac_fire = tx_mac_valid && tx_mac_ready;
    assign tx_status_fire = tx_status_valid && tx_status_ready;
    assign rx_reason = rx_info_data[383:376];
    assign tx_reason = tx_status_data[127:120];
    assign rx_latency = tick - rx_info_data[351:288];

    assign deltas[16*0  +: 16] = 16'(rx_mac_fire && rx_mac_last);
    assign deltas[16*1  +: 16] = rx_mac_fire ? 16'(prefix_bytes(rx_mac_keep)) : 16'd0;
    assign deltas[16*2  +: 16] = 16'(rx_info_fire && rx_reason == `FE_OK);
    assign deltas[16*3  +: 16] = 16'(rx_info_fire && rx_reason != `FE_OK);
    assign deltas[16*4  +: 16] = 16'(rx_mac_valid && !rx_mac_ready);
    assign deltas[16*5  +: 16] = rx_body_fire ? 16'(prefix_bytes(rx_payload_keep)) : 16'd0;
    assign deltas[16*6  +: 16] = 16'(rx_info_fire && rx_reason == `FE_BAD_AXIS);
    assign deltas[16*7  +: 16] = 16'(rx_info_fire && rx_reason == `FE_MAC_BAD);
    assign deltas[16*8  +: 16] = 16'(rx_info_fire && rx_reason == `FE_IP_CHECKSUM);
    assign deltas[16*9  +: 16] = 16'(rx_info_fire && rx_reason == `FE_UDP_CHECKSUM);
    assign deltas[16*10 +: 16] = 16'(rx_info_fire && rx_reason == `FE_RULE_MISS);
    assign deltas[16*11 +: 16] = 16'(rx_info_fire && rx_reason == `FE_RULE_DROP);
    assign deltas[16*12 +: 16] = 16'(rx_info_fire && rx_reason == `FE_OK && rx_info_data[5]);
    assign deltas[16*13 +: 16] = 16'(rx_info_valid && !rx_info_ready);
    assign deltas[16*14 +: 16] = 16'(rx_payload_valid && !rx_payload_ready);
    assign deltas[16*15 +: 16] = 16'(rx_info_fire && rx_reason == `FE_FRAME_LIMIT);
    assign deltas[16*16 +: 16] = 16'(tx_mac_fire && tx_mac_last && !tx_mac_lane);
    assign deltas[16*17 +: 16] = 16'(tx_mac_fire && tx_mac_last && tx_mac_lane);
    assign deltas[16*18 +: 16] = tx_mac_fire ? 16'(prefix_bytes(tx_mac_keep)) : 16'd0;
    assign deltas[16*19 +: 16] = 16'(tx_mac_valid && !tx_mac_ready);
    assign deltas[16*20 +: 16] = 16'(tx_status_fire && tx_reason != `FE_OK);
    assign deltas[16*21 +: 16] = 16'(tx_status_fire && tx_reason == `FE_TX_DESCRIPTOR);
    assign deltas[16*22 +: 16] = 16'(tx_status_fire && tx_reason == `FE_TX_LENGTH);
    assign deltas[16*23 +: 16] = 16'(tx_status_fire && tx_reason == `FE_BAD_AXIS);
    assign deltas[16*24 +: 16] = 16'(tx_status_valid && !tx_status_ready);
    assign deltas[16*25 +: 16] = 16'(rx_info_fire && rx_info_data[7:6] == 2'd1);
    assign deltas[16*26 +: 16] = 16'(rx_info_fire && rx_info_data[7:6] == 2'd2);
    assign deltas[16*27 +: 16] = 16'(rx_info_fire && rx_latency < 64'd32);
    assign deltas[16*28 +: 16] = 16'(rx_info_fire && rx_latency >= 64'd32 && rx_latency < 64'd64);
    assign deltas[16*29 +: 16] = 16'(rx_info_fire && rx_latency >= 64'd64 && rx_latency < 64'd128);
    assign deltas[16*30 +: 16] = 16'(rx_info_fire && rx_latency >= 64'd128 && rx_latency < 64'd256);
    assign deltas[16*31 +: 16] = 16'(rx_info_fire && rx_latency >= 64'd256);

    eth_counter_bank counters (
        .clk, .rst, .deltas, .clear_live(stats_clear_live), .capture(stats_snapshot),
        .read_index(stats_index), .read_value(stats_data), .snapshot_id(stats_snapshot_id)
    );
endmodule
