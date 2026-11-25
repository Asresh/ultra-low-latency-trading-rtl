`timescale 1ns/1ps
`default_nettype none

module eth_routed_udp_frontend #(
    parameter logic [47:0] LOCAL_MAC = 48'h0200_0000_000a,
    parameter logic [31:0] LOCAL_IP = 32'hc000_020a,
    parameter integer MAX_FRAME = 1536,
    parameter integer MAX_PAYLOAD = 1472,
    parameter integer ROUTE_ROWS = 8,
    parameter integer ROUTE_EPOCH_W = 16,
    parameter integer RETRY_CYCLES = 32,
    parameter integer MAX_ATTEMPTS = 3,
    parameter integer ARP_BURST = 2,
    parameter integer HIGH_BURST = 3,
    parameter bit ALLOW_ZERO_UDP = 1'b1,
    localparam integer ROW_W = $clog2(ROUTE_ROWS+1)
) (
    input wire clk,
    input wire rst,
    input wire mac_rx_valid,
    output wire mac_rx_ready,
    input wire [74:0] mac_rx_beat,
    output wire mac_tx_valid,
    input wire mac_tx_ready,
    output wire [73:0] mac_tx_beat,
    output wire [1:0] mac_tx_source,

    input wire rule_write_valid,
    output wire rule_write_ready,
    input wire [2:0] rule_write_index,
    input wire [255:0] rule_write_data,
    input wire rule_commit_valid,
    output wire rule_commit_ready,
    output wire [7:0] rule_epoch,
    output wire rx_info_valid,
    input wire rx_info_ready,
    output wire [383:0] rx_info_data,
    output wire rx_payload_valid,
    input wire rx_payload_ready,
    output wire [63:0] rx_payload_data,
    output wire [7:0] rx_payload_keep,
    output wire rx_payload_first,
    output wire rx_payload_last,

    input wire [1:0] tx_desc_valid,
    output wire [1:0] tx_desc_ready,
    input wire [639:0] tx_desc_data,
    input wire [1:0] tx_allow_multicast,
    input wire [1:0] tx_require_current_epoch,
    input wire [1:0] tx_payload_valid,
    output wire [1:0] tx_payload_ready,
    input wire [127:0] tx_payload_data,
    input wire [15:0] tx_payload_keep,
    input wire [1:0] tx_payload_first,
    input wire [1:0] tx_payload_last,
    output wire tx_status_valid,
    input wire tx_status_ready,
    output wire [127:0] tx_status_data,
    output wire [31:0] tx_status_next_hop,
    output wire [ROW_W-1:0] tx_status_route_row,
    output wire [ROUTE_EPOCH_W-1:0] tx_status_epoch,

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
    output wire [ROUTE_EPOCH_W-1:0] active_epoch,

    input wire status_snapshot,
    input wire [4:0] status_index,
    output wire [31:0] status_read_data,
    output logic [31:0] status_snapshot_id
);
    localparam integer COUNTERS = 23;
    wire ip_rx_valid, ip_rx_ready, ip_tx_valid, ip_tx_ready, ip_tx_lane, mac_tx_ipv4;
    wire [74:0] ip_rx_beat;
    wire [73:0] ip_tx_beat;
    wire [63:0] tick, ip_rx_tick;
    wire route_commit = commit_valid && commit_ready;
    wire route_valid, route_ready, route_lane, route_tagged, route_allow_multicast;
    wire [11:0] route_vid;
    wire [31:0] route_destination;
    wire resolved_valid, resolved_ready, resolved_lane;
    wire [3:0] resolved_status;
    wire [47:0] resolved_mac;
    wire [31:0] resolved_next_hop;
    wire [ROW_W-1:0] resolved_row;
    wire [ROUTE_EPOCH_W-1:0] resolved_epoch;
    wire [1:0] native_desc_valid, native_desc_ready, native_payload_valid, native_payload_ready;
    wire [639:0] native_desc_data;
    wire native_done_valid, native_done_ready;
    wire [127:0] native_done;
    wire unused_tagged;
    wire [11:0] unused_vid;
    wire [31:0] unused_destination;
    wire [63:0] unused_started, unused_finished;
    wire [1:0] pending_routes;
    wire [31:0] live [0:COUNTERS-1];
    logic [31:0] frozen [0:COUNTERS-1];
    logic [31:0] tx_frames [0:2];

    assign mac_tx_source = !mac_tx_valid || !mac_tx_ipv4 ? 2'd0 :
                           ip_tx_lane ? 2'd2 : 2'd1;
    assign live[17] = 32'(pending_routes);
    assign live[18] = 32'(active_epoch);
    assign live[19] = 32'(rule_epoch);
    assign status_read_data = int'(status_index) < COUNTERS ? frozen[status_index] : '0;
    for (genvar lane=0; lane<3; lane=lane+1) begin : transmit_counts
        assign live[20+lane] = tx_frames[lane];
        always_ff @(posedge clk)
            if (rst)
                tx_frames[lane] <= '0;
            else if (mac_tx_valid && mac_tx_ready && mac_tx_beat[72] &&
                     mac_tx_source == 2'(lane) && !(&tx_frames[lane]))
                tx_frames[lane] <= tx_frames[lane] + 1'b1;
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            status_snapshot_id <= '0;
            for (integer item=0; item<COUNTERS; item=item+1)
                frozen[item] <= '0;
        end else if (status_snapshot) begin
            if (!(&status_snapshot_id))
                status_snapshot_id <= status_snapshot_id + 1'b1;
            for (integer item=0; item<COUNTERS; item=item+1)
                frozen[item] <= live[item];
        end
    end

    eth_routed_link #(
        .LOCAL_MAC(LOCAL_MAC), .LOCAL_IP(LOCAL_IP), .SLOTS(2), .ROWS(ROUTE_ROWS),
        .TAG_W(1), .EPOCH_W(ROUTE_EPOCH_W), .ARP_BURST(ARP_BURST),
        .RETRY_CYCLES(RETRY_CYCLES), .MAX_ATTEMPTS(MAX_ATTEMPTS)
    ) link (
        .clk, .rst, .clock_tick(tick), .mac_rx_valid, .mac_rx_ready, .mac_rx_beat,
        .mac_tx_valid, .mac_tx_ready, .mac_tx_beat, .mac_tx_ipv4,
        .ip_rx_valid, .ip_rx_ready, .ip_rx_beat, .ip_rx_tick, .ip_tx_valid, .ip_tx_ready, .ip_tx_beat,
        .s_valid(route_valid), .s_ready(route_ready), .s_tag(route_lane), .s_tagged(route_tagged),
        .s_vid(route_vid), .s_destination(route_destination), .s_allow_multicast(route_allow_multicast),
        .m_valid(resolved_valid), .m_ready(resolved_ready), .m_tag(resolved_lane), .m_tagged(unused_tagged),
        .m_vid(unused_vid), .m_destination(unused_destination), .m_next_hop(resolved_next_hop),
        .m_mac(resolved_mac), .m_status(resolved_status), .m_row(resolved_row), .m_epoch(resolved_epoch),
        .m_start_tick(unused_started), .m_finish_tick(unused_finished), .occupied(pending_routes),
        .unexpected_neighbor_replies(live[0]), .successful_lookups(live[1]), .failed_lookups(live[2]),
        .cfg_valid, .cfg_ready, .cfg_row, .cfg_enabled, .cfg_tagged, .cfg_vid,
        .cfg_network, .cfg_prefix, .cfg_reject, .cfg_gateway, .cfg_error, .commit_valid,
        .commit_ready, .active_epoch,
        .ipv4_frames(live[3]), .arp_frames(live[4]), .other_frames(live[5]), .malformed_prefixes(live[6]),
        .cache_hits(live[7]), .misses(live[8]), .joined(live[9]), .probes_sent(live[10]),
        .replies_learned(live[11]), .replies_ignored(live[12]), .timeouts(live[13]),
        .arp_rx_rejected(live[14]), .arp_rx_replies(live[15]), .arp_tx_answers(live[16])
    );

    eth_rx_path #(.MAX_FRAME(MAX_FRAME), .ALLOW_ZERO_UDP(ALLOW_ZERO_UDP)) receiver (
        .clk, .rst, .timestamp(ip_rx_tick), .mac_valid(ip_rx_valid), .mac_ready(ip_rx_ready),
        .mac_data(ip_rx_beat[63:0]), .mac_keep(ip_rx_beat[71:64]), .mac_first(ip_rx_beat[73]),
        .mac_last(ip_rx_beat[72]), .mac_bad(ip_rx_beat[74]), .rule_write_valid, .rule_write_ready,
        .rule_write_index, .rule_write_data, .rule_commit_valid, .rule_commit_ready, .rule_epoch,
        .info_valid(rx_info_valid), .info_ready(rx_info_ready), .info_data(rx_info_data),
        .payload_valid(rx_payload_valid), .payload_ready(rx_payload_ready), .payload_data(rx_payload_data),
        .payload_keep(rx_payload_keep), .payload_first(rx_payload_first), .payload_last(rx_payload_last)
    );

    eth_udp_route_adapter #(
        .LOCAL_MAC(LOCAL_MAC), .LOCAL_IP(LOCAL_IP), .ROW_W(ROW_W), .EPOCH_W(ROUTE_EPOCH_W),
        .MAX_PAYLOAD(MAX_PAYLOAD)
    ) routed_descriptors (.*);

    eth_tx_path #(.MAX_PAYLOAD(MAX_PAYLOAD), .HIGH_BURST(HIGH_BURST)) transmitter (
        .clk, .rst, .timestamp(tick), .tx_desc_valid(native_desc_valid), .tx_desc_ready(native_desc_ready),
        .tx_desc_data(native_desc_data), .tx_payload_valid(native_payload_valid),
        .tx_payload_ready(native_payload_ready), .tx_payload_data, .tx_payload_keep,
        .tx_payload_first, .tx_payload_last, .tx_mac_valid(ip_tx_valid), .tx_mac_ready(ip_tx_ready),
        .tx_mac_data(ip_tx_beat[63:0]), .tx_mac_keep(ip_tx_beat[71:64]), .tx_mac_first(ip_tx_beat[73]),
        .tx_mac_last(ip_tx_beat[72]), .tx_mac_lane(ip_tx_lane), .tx_status_valid(native_done_valid),
        .tx_status_ready(native_done_ready), .tx_status_data(native_done)
    );
endmodule

`default_nettype wire
