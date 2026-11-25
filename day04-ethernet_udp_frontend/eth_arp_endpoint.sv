`timescale 1ns/1ps
`default_nettype none

module eth_arp_endpoint #(
    parameter logic [47:0] LOCAL_MAC = 48'h0200_0000_000a,
    parameter logic [31:0] LOCAL_IP = 32'hc000_020a,
    parameter integer MAX_FRAME_BYTES = 1536,
    parameter integer CACHE_DEPTH = 8,
    parameter integer PENDING_DEPTH = 4,
    parameter integer WAITERS = 4,
    parameter integer RETRY_CYCLES = 32,
    parameter integer MAX_ATTEMPTS = 3,
    parameter integer CACHE_TTL = 1024,
    parameter integer COOKIE_W = 16
) (
    input wire clk,
    input wire rst,
    input wire s_valid,
    output wire s_ready,
    input wire s_tagged,
    input wire [11:0] s_vid,
    input wire [31:0] s_ip,
    input wire [COOKIE_W-1:0] s_cookie,
    output wire m_valid,
    input wire m_ready,
    output wire [2:0] m_status,
    output wire [COOKIE_W-1:0] m_cookie,
    output wire [47:0] m_mac,
    input wire s_mac_valid,
    output wire s_mac_ready,
    input wire [63:0] s_mac_data,
    input wire [7:0] s_mac_keep,
    input wire s_mac_first,
    input wire s_mac_last,
    input wire s_mac_bad,
    output wire m_mac_valid,
    input wire m_mac_ready,
    output wire [63:0] m_mac_data,
    output wire [7:0] m_mac_keep,
    output wire m_mac_first,
    output wire m_mac_last,
    output wire [31:0] cache_hits,
    output wire [31:0] misses,
    output wire [31:0] joined,
    output wire [31:0] probes_sent,
    output wire [31:0] replies_learned,
    output wire [31:0] replies_ignored,
    output wire [31:0] timeouts,
    output wire [31:0] rx_rejected,
    output wire [31:0] rx_replies,
    output wire [31:0] tx_answers
);
    wire reply_valid, reply_ready, reply_tagged;
    wire [11:0] reply_vid;
    wire [31:0] reply_ip;
    wire [47:0] reply_mac;
    wire probe_valid, probe_ready, probe_tagged;
    wire [11:0] probe_vid;
    wire [31:0] probe_ip;

    eth_arp_resolver #(
        .CACHE_DEPTH(CACHE_DEPTH), .PENDING_DEPTH(PENDING_DEPTH), .WAITERS(WAITERS),
        .RETRY_CYCLES(RETRY_CYCLES), .MAX_ATTEMPTS(MAX_ATTEMPTS), .CACHE_TTL(CACHE_TTL),
        .COOKIE_W(COOKIE_W)
    ) resolver (
        .clk, .rst, .s_valid, .s_ready, .s_tagged, .s_vid, .s_ip, .s_cookie,
        .m_valid, .m_ready, .m_status, .m_cookie, .m_mac,
        .s_reply_valid(reply_valid), .s_reply_ready(reply_ready),
        .s_reply_tagged(reply_tagged), .s_reply_vid(reply_vid), .s_reply_ip(reply_ip),
        .s_reply_mac(reply_mac),
        .m_probe_valid(probe_valid), .m_probe_ready(probe_ready), .m_probe_tagged(probe_tagged),
        .m_probe_vid(probe_vid), .m_probe_ip(probe_ip),
        .cache_hits, .misses, .joined, .probes_sent, .replies_learned, .replies_ignored, .timeouts
    );

    eth_arp_wire #(
        .LOCAL_MAC(LOCAL_MAC), .LOCAL_IP(LOCAL_IP), .MAX_FRAME_BYTES(MAX_FRAME_BYTES)
    ) arp_port (
        .clk, .rst, .s_mac_valid, .s_mac_ready, .s_mac_data, .s_mac_keep, .s_mac_first,
        .s_mac_last, .s_mac_bad, .m_mac_valid, .m_mac_ready, .m_mac_data, .m_mac_keep,
        .m_mac_first, .m_mac_last,
        .s_probe_valid(probe_valid), .s_probe_ready(probe_ready), .s_probe_tagged(probe_tagged),
        .s_probe_vid(probe_vid), .s_probe_ip(probe_ip),
        .m_reply_valid(reply_valid), .m_reply_ready(reply_ready), .m_reply_tagged(reply_tagged),
        .m_reply_vid(reply_vid), .m_reply_ip(reply_ip), .m_reply_mac(reply_mac),
        .rx_rejected, .rx_replies, .tx_answers
    );
endmodule

`default_nettype wire
