`timescale 1ns/1ps

module eth_tx_header (
    input  logic [47:0]  mac_destination,
    input  logic [47:0]  mac_source,
    input  logic [31:0]  ip_source,
    input  logic [31:0]  ip_destination,
    input  logic [15:0]  udp_source,
    input  logic [15:0]  udp_destination,
    input  logic [15:0]  payload_bytes,
    input  logic [15:0]  outer_tci,
    input  logic [15:0]  inner_tci,
    input  logic [15:0]  identification,
    input  logic [7:0]   ttl,
    input  logic [7:0]   tos,
    input  logic [1:0]   vlan_depth,
    input  logic         udp_disable,
    input  logic         df,
    input  logic [31:0]  payload_sum,
    output logic [399:0] header_bytes,
    output logic [15:0]  header_length
);
    `include "frontend_words.svh"

    logic [15:0] udp_length, ip_length, ip_flag_word, ip_checksum, udp_checksum;
    logic [15:0] udp_unencoded;
    logic [31:0] ip_total_sum, udp_total_sum;
    logic [159:0] ip_header;
    logic [63:0] udp_header;
    logic [399:0] header_network_order;

    assign udp_length = payload_bytes + 16'd8;
    assign ip_length = payload_bytes + 16'd28;
    assign ip_flag_word = df ? 16'h4000 : 16'h0;
    assign ip_total_sum = {16'd0,8'h45,tos} + {16'd0,ip_length} + {16'd0,identification}
                        + {16'd0,ip_flag_word} + {16'd0,ttl,8'd17}
                        + {16'd0,ip_source[31:16]} + {16'd0,ip_source[15:0]}
                        + {16'd0,ip_destination[31:16]} + {16'd0,ip_destination[15:0]};
    assign ip_checksum = ~fold_sum(ip_total_sum);

    assign udp_total_sum = payload_sum + pseudo_sum(ip_source, ip_destination, udp_length)
                         + {16'd0,udp_source} + {16'd0,udp_destination}
                         + {16'd0,udp_length};
    assign udp_unencoded = ~fold_sum(udp_total_sum);
    assign udp_checksum = udp_disable ? 16'd0
                         : udp_unencoded == 0 ? 16'hffff : udp_unencoded;

    assign ip_header = {
        8'h45, tos, ip_length, identification, ip_flag_word, ttl, 8'd17,
        ip_checksum, ip_source, ip_destination
    };
    assign udp_header = {udp_source, udp_destination, udp_length, udp_checksum};

    always @* begin
        case (vlan_depth)
            2'd0: begin
                header_network_order = {
                    mac_destination, mac_source, 16'h0800, ip_header, udp_header, 64'd0
                };
                header_length = 16'd42;
            end
            2'd1: begin
                header_network_order = {
                    mac_destination, mac_source, 16'h8100, outer_tci, 16'h0800,
                    ip_header, udp_header, 32'd0
                };
                header_length = 16'd46;
            end
            default: begin
                header_network_order = {
                    mac_destination, mac_source, 16'h88a8, outer_tci, 16'h8100, inner_tci,
                    16'h0800, ip_header, udp_header
                };
                header_length = 16'd50;
            end
        endcase
    end

    for (genvar byte_index = 0; byte_index < 50; byte_index = byte_index+1) begin : g_lane
        assign header_bytes[8*byte_index +: 8] = header_network_order[399-8*byte_index -: 8];
    end
endmodule
