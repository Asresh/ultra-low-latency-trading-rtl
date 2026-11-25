`timescale 1ns/1ps
`default_nettype none

// Common MAC, neighbor service and route-to-MAC query interface.
// The legacy IPv4 UDP client attaches to the two ip_* MAC streams.
module eth_routed_link #(
    parameter logic [47:0] LOCAL_MAC = 48'h0200_0000_000a,
    parameter logic [31:0] LOCAL_IP = 32'hc000_020a,
    parameter integer SLOTS = 8,
    parameter integer ROWS = 8,
    parameter integer TAG_W = 32,
    parameter integer EPOCH_W = 16,
    parameter integer CACHE_DEPTH = 8,
    parameter integer PENDING_DEPTH = 4,
    parameter integer WAITERS = 4,
    parameter integer RETRY_CYCLES = 32,
    parameter integer MAX_ATTEMPTS = 3,
    parameter integer CACHE_TTL = 1024,
    parameter integer ARP_BURST = 2,
    localparam integer SLOT_W = SLOTS > 1 ? $clog2(SLOTS) : 1,
    localparam integer ROW_W = $clog2(ROWS+1),
    localparam integer COUNT_W = $clog2(SLOTS+1)
) (
    input wire clk,
    input wire rst,
    output logic [63:0] clock_tick,
    input wire mac_rx_valid,
    output wire mac_rx_ready,
    input wire [74:0] mac_rx_beat,
    output wire mac_tx_valid,
    input wire mac_tx_ready,
    output wire [73:0] mac_tx_beat,
    output wire mac_tx_ipv4,

    output wire ip_rx_valid,
    input wire ip_rx_ready,
    output wire [74:0] ip_rx_beat,
    output wire [63:0] ip_rx_tick,
    input wire ip_tx_valid,
    output wire ip_tx_ready,
    input wire [73:0] ip_tx_beat,

    input wire s_valid,
    output wire s_ready,
    input wire [TAG_W-1:0] s_tag,
    input wire s_tagged,
    input wire [11:0] s_vid,
    input wire [31:0] s_destination,
    input wire s_allow_multicast,
    output wire m_valid,
    input wire m_ready,
    output wire [TAG_W-1:0] m_tag,
    output wire m_tagged,
    output wire [11:0] m_vid,
    output wire [31:0] m_destination,
    output wire [31:0] m_next_hop,
    output wire [47:0] m_mac,
    output wire [3:0] m_status,
    output wire [ROW_W-1:0] m_row,
    output wire [EPOCH_W-1:0] m_epoch,
    output wire [63:0] m_start_tick,
    output wire [63:0] m_finish_tick,
    output wire [COUNT_W-1:0] occupied,
    output wire [31:0] unexpected_neighbor_replies,
    output wire [31:0] successful_lookups,
    output wire [31:0] failed_lookups,

    input wire cfg_valid,
    output wire cfg_ready,
    input wire [ROW_W-1:0] cfg_row,
    input wire cfg_enabled,
    input wire cfg_tagged,
    input wire [11:0] cfg_vid,
    input wire [31:0] cfg_network,
    input wire [5:0] cfg_prefix,
    input wire cfg_reject,
    input wire [31:0] cfg_gateway,
    output wire cfg_error,
    input wire commit_valid,
    output wire commit_ready,
    output wire [EPOCH_W-1:0] active_epoch,

    output wire [31:0] ipv4_frames,
    output wire [31:0] arp_frames,
    output wire [31:0] other_frames,
    output wire [31:0] malformed_prefixes,
    output wire [31:0] cache_hits,
    output wire [31:0] misses,
    output wire [31:0] joined,
    output wire [31:0] probes_sent,
    output wire [31:0] replies_learned,
    output wire [31:0] replies_ignored,
    output wire [31:0] timeouts,
    output wire [31:0] arp_rx_rejected,
    output wire [31:0] arp_rx_replies,
    output wire [31:0] arp_tx_answers
);
    wire [74:0] incoming;
    wire arp_rx_valid, arp_rx_ready, arp_tx_valid, arp_tx_ready;
    wire [73:0] arp_tx_beat;
    wire arp_request_valid, arp_request_ready, arp_request_tagged;
    wire [11:0] arp_request_vid;
    wire [31:0] arp_request_ip;
    wire [SLOT_W-1:0] arp_request_slot, arp_result_slot;
    wire arp_result_valid, arp_result_ready;
    wire [2:0] arp_result_status;
    wire [47:0] arp_result_mac;

    always_ff @(posedge clk)
        if (rst)
            clock_tick <= '0;
        else
            clock_tick <= clock_tick + 1'b1;

    eth_mac_dispatch dispatch (
        .clk, .rst, .clock_tick, .s_valid(mac_rx_valid), .s_ready(mac_rx_ready),
        .s_beat(mac_rx_beat), .m_valid({arp_rx_valid,ip_rx_valid}),
        .m_ready({arp_rx_ready,ip_rx_ready}), .m_beat(incoming), .m_tick(ip_rx_tick),
        .ipv4_frames, .arp_frames, .other_frames, .malformed_prefixes
    );
    assign ip_rx_beat = ip_rx_valid ? incoming : 75'd0;

    eth_next_hop_pipeline #(.SLOTS(SLOTS), .ROWS(ROWS), .TAG_W(TAG_W), .EPOCH_W(EPOCH_W)) planner (.*);

    eth_arp_endpoint #(
        .LOCAL_MAC(LOCAL_MAC), .LOCAL_IP(LOCAL_IP), .CACHE_DEPTH(CACHE_DEPTH),
        .PENDING_DEPTH(PENDING_DEPTH), .WAITERS(WAITERS), .RETRY_CYCLES(RETRY_CYCLES),
        .MAX_ATTEMPTS(MAX_ATTEMPTS), .CACHE_TTL(CACHE_TTL), .COOKIE_W(SLOT_W)
    ) arp (
        .clk, .rst, .s_valid(arp_request_valid), .s_ready(arp_request_ready),
        .s_tagged(arp_request_tagged), .s_vid(arp_request_vid), .s_ip(arp_request_ip),
        .s_cookie(arp_request_slot), .m_valid(arp_result_valid), .m_ready(arp_result_ready),
        .m_status(arp_result_status), .m_cookie(arp_result_slot), .m_mac(arp_result_mac),
        .s_mac_valid(arp_rx_valid), .s_mac_ready(arp_rx_ready),
        .s_mac_data(incoming[63:0]), .s_mac_keep(incoming[71:64]),
        .s_mac_first(incoming[73]), .s_mac_last(incoming[72]), .s_mac_bad(incoming[74]),
        .m_mac_valid(arp_tx_valid), .m_mac_ready(arp_tx_ready),
        .m_mac_data(arp_tx_beat[63:0]), .m_mac_keep(arp_tx_beat[71:64]),
        .m_mac_first(arp_tx_beat[73]), .m_mac_last(arp_tx_beat[72]),
        .cache_hits, .misses, .joined, .probes_sent, .replies_learned, .replies_ignored,
        .timeouts, .rx_rejected(arp_rx_rejected), .rx_replies(arp_rx_replies),
        .tx_answers(arp_tx_answers)
    );

    eth_packet_arbiter #(.HIGH_BURST(ARP_BURST), .WIDTH(74)) output_mac (
        .clk, .rst, .in_valid({ip_tx_valid,arp_tx_valid}),
        .in_ready({ip_tx_ready,arp_tx_ready}), .in_last({ip_tx_beat[72],arp_tx_beat[72]}),
        .in_data({ip_tx_beat,arp_tx_beat}), .out_valid(mac_tx_valid), .out_ready(mac_tx_ready),
        .out_data(mac_tx_beat), .out_lane(mac_tx_ipv4)
    );
endmodule

`default_nettype wire
