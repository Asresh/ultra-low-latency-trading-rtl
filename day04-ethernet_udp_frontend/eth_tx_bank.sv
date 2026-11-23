`timescale 1ns/1ps
`include "frontend_codes.svh"

module eth_tx_bank #(
    parameter integer MAX_PAYLOAD = 1472
) (
    input  logic         clk,
    input  logic         rst,
    input  logic [63:0]  timestamp,

    input  logic         desc_valid,
    output logic         desc_ready,
    input  logic [319:0] desc_data,
    input  logic         s_valid,
    output logic         s_ready,
    input  logic [63:0]  s_data,
    input  logic [7:0]   s_keep,
    input  logic         s_first,
    input  logic         s_last,

    output logic         m_valid,
    input  logic         m_ready,
    output logic [63:0]  m_data,
    output logic [7:0]   m_keep,
    output logic         m_first,
    output logic         m_last,

    output logic         done_valid,
    input  logic         done_ready,
    output logic [127:0] done_data
);
    `include "frontend_words.svh"

    localparam integer ADDRESS_BITS = $clog2((MAX_PAYLOAD+7)/8);
    localparam logic [16:0] PAYLOAD_LIMIT = 17'(MAX_PAYLOAD);

    typedef enum logic [2:0] {IDLE, COLLECT, BUILD, SEND_FRAME, REPORT} tx_state_t;
    tx_state_t state;

    logic [303:0] descriptor_fields;
    logic [3:0] descriptor_options;
    logic [31:0] cookie;
    logic [47:0] destination_mac, source_mac;
    logic [31:0] source_ip, destination_ip;
    logic [15:0] source_port, destination_port, declared_length;
    logic [15:0] outer_tag, inner_tag, identification;
    logic [7:0] ip_ttl, ip_tos;
    logic [1:0] vlan_depth;
    logic udp_disable, df;
    logic desc_fault, capture_fault, capture_axis_fault, has_payload_beat;
    logic [15:0] cursor, logical_frame_length, wire_frame_length, send_position;
    logic [15:0] capture_bytes;
    logic [16:0] incoming_length;
    logic [3:0] keep_size;
    logic [31:0] checksum_payload;
    logic [63:0] status_tick;
    logic [7:0] finish_code;

    logic packet_take, payload_take, beat_fire, write_payload;
    logic new_desc_fault, bad_beat, length_fault_now;
    logic [399:0] header_preview, header;
    logic [15:0] header_preview_length, header_length;
    logic [63:0] header_word, payload_word, payload_shifted;
    logic [15:0] requested_position, request_payload_offset, requested_frame_end;
    logic [2:0] read_shift, head_skip;
    logic read_enable;
    logic [127:0] payload_pair;

    assign desc_ready = !rst && state == IDLE;
    assign packet_take = desc_valid && desc_ready;
    assign s_ready = !rst && state == COLLECT;
    assign payload_take = s_valid && s_ready;
    assign beat_fire = m_valid && m_ready;
    assign m_valid = !rst && state == SEND_FRAME;
    assign m_first = send_position == 0;
    assign m_last = send_position + 16'd8 >= wire_frame_length;

    assign {cookie, destination_mac, source_mac, source_ip, destination_ip,
            source_port, destination_port, declared_length, outer_tag, inner_tag,
            identification, ip_ttl, ip_tos} = descriptor_fields;
    assign vlan_depth  = descriptor_options[3:2];
    assign udp_disable = descriptor_options[1];
    assign df          = descriptor_options[0];

    assign new_desc_fault = ({1'b0,desc_data[95:80]} > PAYLOAD_LIMIT)
                         || (desc_data[31:24] == 0) || (desc_data[7:6] == 2'd3)
                         || (desc_data[15:8] != 0) || (desc_data[3:0] != 0);
    assign keep_size = prefix_bytes(s_keep);
    assign incoming_length = {1'b0,capture_bytes} + {13'd0,s_last ? keep_size : 4'd8};
    assign bad_beat = s_first != !has_payload_beat
                   || (s_last ? keep_size == 0 : s_keep != 8'hff);
    assign length_fault_now = capture_fault || incoming_length > {1'b0,declared_length};
    assign write_payload = payload_take && !capture_fault && !desc_fault
                        && incoming_length <= {1'b0,declared_length};

    eth_tx_header format_header (
        .mac_destination(destination_mac), .mac_source(source_mac),
        .ip_source(source_ip), .ip_destination(destination_ip),
        .udp_source(source_port), .udp_destination(destination_port),
        .payload_bytes(declared_length), .outer_tci(outer_tag), .inner_tci(inner_tag),
        .identification, .ttl(ip_ttl), .tos(ip_tos), .vlan_depth, .udp_disable, .df,
        .payload_sum(checksum_payload), .header_bytes(header_preview),
        .header_length(header_preview_length)
    );

    assign requested_position = send_position + 16'd8;
    assign requested_frame_end = requested_position + 16'd8;
    assign request_payload_offset = requested_position < header_length
                                  ? 16'd0 : requested_position-header_length;
    assign read_enable = beat_fire && !m_last
                      && requested_frame_end > header_length
                      && request_payload_offset < declared_length;

    frontend_pair_ram #(.ADDRESS_BITS(ADDRESS_BITS)) payload_memory (
        .clk, .write_enable(write_payload),
        .write_address(ADDRESS_BITS'(cursor >> 3)), .write_data(s_data),
        .read_enable, .read_address(ADDRESS_BITS'(request_payload_offset >> 3)),
        .read_pair(payload_pair)
    );

    always_ff @(posedge clk)
        if (read_enable)
            read_shift <= request_payload_offset[2:0];

    assign head_skip = 3'(header_length-send_position);
    assign payload_word = 64'(payload_pair >> {read_shift,3'b000});
    assign payload_shifted = send_position < header_length
                           ? payload_word << {head_skip,3'b000} : payload_word;
    assign header_word = send_position < header_length
                       ? 64'(header >> {send_position[5:0],3'b000}) : 64'd0;

    for (genvar lane = 0; lane < 8; lane = lane+1) begin : g_output
        logic [15:0] absolute_lane;
        assign absolute_lane = send_position + 16'(lane);
        assign m_keep[lane] = absolute_lane < wire_frame_length;
        assign m_data[lane*8 +: 8] =
            absolute_lane < header_length ? header_word[lane*8 +: 8]
          : absolute_lane < logical_frame_length ? payload_shifted[lane*8 +: 8] : 8'd0;
    end

    assign done_valid = !rst && state == REPORT;
    assign done_data = {finish_code, 8'd0, cookie, wire_frame_length, status_tick};

    always_ff @(posedge clk) begin
        if (rst) begin
            state                <= IDLE;
            descriptor_fields    <= '0;
            descriptor_options   <= '0;
            desc_fault           <= 1'b0;
            capture_fault        <= 1'b0;
            capture_axis_fault   <= 1'b0;
            has_payload_beat     <= 1'b0;
            cursor               <= '0;
            logical_frame_length <= '0;
            wire_frame_length    <= '0;
            send_position        <= '0;
            capture_bytes        <= '0;
            checksum_payload     <= '0;
            header               <= '0;
            header_length        <= '0;
            status_tick          <= '0;
            finish_code          <= `FE_OK;
        end else begin
            case (state)
                IDLE: if (packet_take) begin
                    descriptor_fields <= desc_data[319:16];
                    descriptor_options <= desc_data[7:4];
                    desc_fault        <= new_desc_fault;
                    capture_fault     <= 1'b0;
                    capture_axis_fault <= 1'b0;
                    has_payload_beat  <= 1'b0;
                    cursor            <= '0;
                    capture_bytes     <= '0;
                    checksum_payload  <= '0;
                    wire_frame_length <= '0;
                    status_tick       <= timestamp;
                    finish_code       <= `FE_OK;
                    if (desc_data[95:80] != 0)
                        state <= COLLECT;
                    else if (new_desc_fault) begin
                        finish_code <= `FE_TX_DESCRIPTOR;
                        state       <= REPORT;
                    end else begin
                        state <= BUILD;
                    end
                end
                COLLECT: if (payload_take) begin
                    has_payload_beat  <= 1'b1;
                    capture_axis_fault <= capture_axis_fault || bad_beat;
                    capture_fault     <= length_fault_now;
                    if (write_payload) begin
                        cursor           <= cursor + 16'd8;
                        capture_bytes    <= incoming_length[15:0];
                        checksum_payload <= sum_word(checksum_payload, s_data, s_last ? keep_size : 4'd8);
                    end
                    if (s_last) begin
                        if (desc_fault) begin
                            finish_code <= `FE_TX_DESCRIPTOR;
                            state       <= REPORT;
                        end else if (capture_axis_fault || bad_beat) begin
                            finish_code <= `FE_BAD_AXIS;
                            state       <= REPORT;
                        end else if (length_fault_now || incoming_length != {1'b0,declared_length}) begin
                            finish_code <= `FE_TX_LENGTH;
                            state       <= REPORT;
                        end else begin
                            state <= BUILD;
                        end
                    end
                end
                BUILD: begin
                    header               <= header_preview;
                    header_length        <= header_preview_length;
                    logical_frame_length <= header_preview_length + declared_length;
                    wire_frame_length    <= header_preview_length + declared_length < 16'd60
                                           ? 16'd60 : header_preview_length + declared_length;
                    send_position        <= 16'd0;
                    state                <= SEND_FRAME;
                end
                SEND_FRAME: if (beat_fire) begin
                    if (m_first)
                        status_tick <= timestamp;
                    if (m_last)
                        state <= REPORT;
                    else
                        send_position <= requested_position;
                end
                REPORT: if (done_ready)
                    state <= IDLE;
                default: state <= IDLE;
            endcase
        end
    end
endmodule
