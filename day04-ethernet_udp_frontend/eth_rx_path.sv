`timescale 1ns/1ps
`include "frontend_codes.svh"

module eth_rx_path #(
    parameter integer MAX_FRAME = 1536,
    parameter bit ALLOW_ZERO_UDP = 1'b1
) (
    input  logic         clk,
    input  logic         rst,
    input  logic [63:0]  timestamp,

    input  logic         mac_valid,
    output logic         mac_ready,
    input  logic [63:0]  mac_data,
    input  logic [7:0]   mac_keep,
    input  logic         mac_first,
    input  logic         mac_last,
    input  logic         mac_bad,

    input  logic         rule_write_valid,
    output logic         rule_write_ready,
    input  logic [2:0]   rule_write_index,
    input  logic [255:0] rule_write_data,
    input  logic         rule_commit_valid,
    output logic         rule_commit_ready,
    output logic [7:0]   rule_epoch,

    output logic         info_valid,
    input  logic         info_ready,
    output logic [383:0] info_data,

    output logic         payload_valid,
    input  logic         payload_ready,
    output logic [63:0]  payload_data,
    output logic [7:0]   payload_keep,
    output logic         payload_first,
    output logic         payload_last
);
    typedef enum logic [1:0] {WAIT_BANK, REPORT, REPLAY} dispatch_state_t;
    dispatch_state_t state;

    logic capture_bank, read_bank, capture_last, meta_fire, data_last;
    logic [1:0] bank_in_valid, bank_in_ready, bank_inspect_valid, bank_inspect_take;
    logic [1:0] bank_payload_valid, bank_payload_ready, bank_payload_first, bank_payload_last;
    logic [767:0] bank_inspect_data;
    logic [127:0] bank_payload_data;
    logic [15:0] bank_payload_keep;
    logic [383:0] inspected_with_unused_fields;
    logic route_valid, route_drop, allow, result_ok, output_payload;
    logic [2:0] route_queue;
    logic [7:0] resolved_reason;

    assign mac_ready = bank_in_ready[capture_bank];
    assign capture_last = mac_valid && mac_ready && mac_last;
    assign bank_in_valid = mac_valid ? (2'b01 << capture_bank) : 2'b00;
    assign inspected_with_unused_fields = bank_inspect_data[384*read_bank +: 384];
    assign info_valid = !rst && state == REPORT;
    assign meta_fire = info_valid && info_ready;
    assign result_ok = info_data[383:376] == `FE_OK;
    assign output_payload = result_ok && info_data[95:80] != 0;
    assign bank_inspect_take = meta_fire ? (2'b01 << read_bank) : 2'b00;
    assign allow = meta_fire && result_ok;

    assign payload_valid = !rst && state == REPLAY && bank_payload_valid[read_bank];
    assign payload_data = bank_payload_data[64*read_bank +: 64];
    assign payload_keep = bank_payload_keep[8*read_bank +: 8];
    assign payload_first = bank_payload_first[read_bank];
    assign payload_last = bank_payload_last[read_bank];
    assign bank_payload_ready = state == REPLAY && payload_ready ? (2'b01 << read_bank) : 2'b00;
    assign data_last = payload_valid && payload_ready && payload_last;

    assign resolved_reason = inspected_with_unused_fields[383:376] != `FE_OK ? inspected_with_unused_fields[383:376]
                           : !route_valid ? `FE_RULE_MISS : route_drop ? `FE_RULE_DROP : `FE_OK;

    eth_rule_table table_rules (
        .clk, .rst, .write_valid(rule_write_valid), .write_ready(rule_write_ready),
        .write_index(rule_write_index), .write_rule(rule_write_data),
        .commit_valid(rule_commit_valid), .commit_ready(rule_commit_ready),
        .active_epoch(rule_epoch),
        .source_ip(inspected_with_unused_fields[191:160]), .destination_ip(inspected_with_unused_fields[159:128]),
        .source_port(inspected_with_unused_fields[127:112]), .destination_port(inspected_with_unused_fields[111:96]),
        .outer_tci(inspected_with_unused_fields[63:48]), .vlan_depth(inspected_with_unused_fields[7:6]),
        .match_valid(route_valid), .match_drop(route_drop), .match_queue(route_queue)
    );

    for (genvar index = 0; index < 2; index = index+1) begin : g_bank
        eth_rx_bank #(.MAX_FRAME(MAX_FRAME), .ALLOW_ZERO_UDP(ALLOW_ZERO_UDP)) receiver (
            .clk, .rst, .timestamp,
            .mac_valid(bank_in_valid[index]), .mac_ready(bank_in_ready[index]),
            .mac_data, .mac_keep, .mac_first, .mac_last, .mac_bad,
            .inspect_valid(bank_inspect_valid[index]), .inspect_take(bank_inspect_take[index]),
            .inspect_allow(allow), .inspect_data(bank_inspect_data[384*index +: 384]),
            .m_valid(bank_payload_valid[index]), .m_ready(bank_payload_ready[index]),
            .m_data(bank_payload_data[64*index +: 64]), .m_keep(bank_payload_keep[8*index +: 8]),
            .m_first(bank_payload_first[index]), .m_last(bank_payload_last[index])
        );
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            capture_bank <= 1'b0;
            read_bank    <= 1'b0;
            info_data    <= '0;
            state        <= WAIT_BANK;
        end else begin
            if (capture_last)
                capture_bank <= !capture_bank;
            case (state)
                WAIT_BANK: if (bank_inspect_valid[read_bank]) begin
                    info_data <= {
                        resolved_reason,
                        resolved_reason == `FE_OK ? {5'd0,route_queue} : 8'd0,
                        rule_epoch, inspected_with_unused_fields[359:0]
                    };
                    state <= REPORT;
                end
                REPORT: if (meta_fire) begin
                    if (output_payload)
                        state <= REPLAY;
                    else begin
                        read_bank <= !read_bank;
                        state <= WAIT_BANK;
                    end
                end
                REPLAY: if (data_last) begin
                    read_bank <= !read_bank;
                    state <= WAIT_BANK;
                end
                default: state <= WAIT_BANK;
            endcase
        end
    end
endmodule
