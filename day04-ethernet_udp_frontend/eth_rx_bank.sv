`timescale 1ns/1ps
`include "frontend_codes.svh"

module eth_rx_bank #(
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

    output logic         inspect_valid,
    input  logic         inspect_take,
    input  logic         inspect_allow,
    output logic [383:0] inspect_data,

    output logic         m_valid,
    input  logic         m_ready,
    output logic [63:0]  m_data,
    output logic [7:0]   m_keep,
    output logic         m_first,
    output logic         m_last
);
    `include "frontend_words.svh"

    localparam integer ADDRESS_BITS = $clog2((MAX_FRAME+7)/8);
    localparam logic [16:0] FRAME_LIMIT = 17'(MAX_FRAME);

    typedef enum logic [4:0] {
        CAPTURE, START, L2_REQUEST, L2_VALUE, IP0_REQUEST, IP0_VALUE,
        IP1_REQUEST, IP1_VALUE, IP2_REQUEST, IP2_VALUE,
        IP_SUM_FIRST, IP_SUM_NEXT, UDP_REQUEST, UDP_VALUE,
        UDP_SUM_FIRST, UDP_SUM_NEXT, OFFER, PAYLOAD_REQUEST, PAYLOAD_VALUE
    } receive_state_t;

    receive_state_t state;
    logic in_packet, capture_bad, capture_oversize, capture_mac_error;
    logic [15:0] captured, wire_length;
    logic [63:0] arrival_tick;
    logic [47:0] mac_destination, mac_source;
    logic [15:0] l2_cursor, ip_start, udp_start, payload_start;
    logic [15:0] outer_tci, inner_tci;
    logic [1:0] vlan_count;
    logic [15:0] ip_total, ip_ident, udp_length;
    logic [3:0] ip_ihl;
    logic df, udp_zero;
    logic [7:0] ip_ttl, ip_protocol;
    logic [31:0] ip_source, ip_destination;
    logic [15:0] udp_source, udp_destination, payload_length;
    logic [7:0] reason;

    logic capture_fire, store_word, frame_fault;
    logic [16:0] capture_end;
    logic [3:0] accepted_bytes, scan_bytes;
    logic [63:0] net_mac, lane_word, net_word;
    logic [15:0] l2_type, l2_tci;
    logic [16:0] ip_end;
    logic [15:0] ipv4_header_bytes, current_ipv4_bytes;
    logic [15:0] minimum_frame;
    logic [15:0] scan_position, remaining;
    logic [31:0] accumulator, summed;
    logic rd, finish_payload;
    logic [15:0] rd_position;
    logic [2:0] rd_shift;
    logic [127:0] read_pair;

    assign mac_ready = !rst && state == CAPTURE;
    assign capture_fire = mac_valid && mac_ready;
    assign accepted_bytes = prefix_bytes(mac_keep);
    assign capture_end = {1'b0,captured} + {13'd0, mac_last ? accepted_bytes : 4'd8};
    assign frame_fault = (mac_first != !in_packet)
                      || (mac_last ? accepted_bytes == 0 : mac_keep != 8'hff);
    assign store_word = capture_fire && !capture_oversize && capture_end <= FRAME_LIMIT;
    assign net_mac = wire_big(mac_data);

    frontend_pair_ram #(.ADDRESS_BITS(ADDRESS_BITS)) memory (
        .clk, .write_enable(store_word),
        .write_address(ADDRESS_BITS'(captured >> 3)), .write_data(mac_data),
        .read_enable(rd), .read_address(ADDRESS_BITS'(rd_position >> 3)),
        .read_pair
    );

    always_ff @(posedge clk)
        if (rd)
            rd_shift <= rd_position[2:0];

    always @* begin
        rd = 1'b1;
        case (state)
            L2_REQUEST: rd_position = l2_cursor;
            IP0_REQUEST: rd_position = ip_start;
            IP1_REQUEST: rd_position = ip_start + 16'd8;
            IP2_REQUEST: rd_position = ip_start + 16'd16;
            UDP_REQUEST: rd_position = udp_start;
            IP_SUM_FIRST, UDP_SUM_FIRST,
            PAYLOAD_REQUEST: rd_position = scan_position;
            IP_SUM_NEXT, UDP_SUM_NEXT: begin
                rd_position = scan_position + 16'd8;
                rd = remaining > 16'd8;
            end
            PAYLOAD_VALUE: begin
                rd_position = scan_position + 16'd8;
                rd = m_ready && remaining > 16'd8;
            end
            default: begin
                rd_position = 16'd0;
                rd = 1'b0;
            end
        endcase
    end

    assign lane_word = 64'(read_pair >> {rd_shift,3'b000});
    assign net_word = wire_big(lane_word);
    assign l2_type = net_word[63:48];
    assign l2_tci = net_word[47:32];
    assign current_ipv4_bytes = {10'd0, net_word[59:56], 2'd0};
    assign ipv4_header_bytes = {10'd0, ip_ihl, 2'd0};
    assign ip_end = {1'b0,ip_start} + {1'b0,ip_total};
    assign minimum_frame = ip_end < 17'd60 ? 16'd60 : ip_end[15:0];
    assign scan_bytes = remaining >= 16'd8 ? 4'd8 : remaining[3:0];
    assign summed = sum_word(accumulator, lane_word, scan_bytes);

    assign inspect_valid = !rst && state == OFFER;
    assign inspect_data = {
        reason, 8'd0, 8'd0, ip_start[7:0], arrival_tick,
        mac_destination, mac_source, ip_source, ip_destination,
        udp_source, udp_destination, payload_length, wire_length,
        outer_tci, inner_tci, ip_ident, ip_ttl, vlan_count, udp_zero, df, ip_ihl
    };

    assign m_valid = !rst && state == PAYLOAD_VALUE;
    assign m_first = scan_position == payload_start;
    assign m_last = remaining <= 16'd8;
    assign finish_payload = m_valid && m_ready && m_last;

    for (genvar lane = 0; lane < 8; lane = lane+1) begin : g_payload_keep
        assign m_keep[lane] = remaining > 16'(lane);
        assign m_data[8*lane +: 8] = m_keep[lane] ? lane_word[8*lane +: 8] : 8'd0;
    end

    task automatic reject(input logic [7:0] code);
        begin
            reason <= code;
            state <= OFFER;
        end
    endtask

    task automatic recycle;
        begin
            state             <= CAPTURE;
            in_packet         <= 1'b0;
            capture_bad       <= 1'b0;
            capture_oversize  <= 1'b0;
            capture_mac_error <= 1'b0;
            captured          <= '0;
        end
    endtask

    always_ff @(posedge clk) begin
        if (rst) begin
            recycle();
            wire_length     <= '0;
            arrival_tick    <= '0;
            mac_destination <= '0;
            mac_source      <= '0;
            l2_cursor       <= '0;
            ip_start        <= '0;
            udp_start       <= '0;
            payload_start   <= '0;
            outer_tci       <= '0;
            inner_tci       <= '0;
            vlan_count      <= '0;
            ip_total        <= '0;
            ip_ident        <= '0;
            ip_ihl          <= '0;
            df              <= 1'b0;
            udp_zero        <= 1'b0;
            ip_ttl          <= '0;
            ip_protocol     <= '0;
            ip_source       <= '0;
            ip_destination  <= '0;
            udp_source      <= '0;
            udp_destination <= '0;
            udp_length      <= '0;
            payload_length  <= '0;
            scan_position   <= '0;
            remaining       <= '0;
            accumulator     <= '0;
            reason          <= `FE_OK;
        end else begin
            case (state)
                CAPTURE: if (capture_fire) begin
                    capture_bad       <= capture_bad || frame_fault;
                    capture_mac_error <= capture_mac_error || mac_bad;
                    capture_oversize  <= capture_oversize || capture_end > FRAME_LIMIT;
                    in_packet         <= !mac_last;
                    if (!capture_oversize && capture_end <= FRAME_LIMIT)
                        captured <= capture_end[15:0];
                    if (!in_packet) begin
                        arrival_tick    <= timestamp;
                        mac_destination <= net_mac[63:16];
                        mac_source      <= {net_mac[15:0],32'd0};
                        ip_start        <= '0;
                        outer_tci       <= '0;
                        inner_tci       <= '0;
                        vlan_count      <= '0;
                        ip_ident        <= '0;
                        ip_ihl          <= '0;
                        df              <= 1'b0;
                        udp_zero        <= 1'b0;
                        ip_ttl          <= '0;
                        ip_source       <= '0;
                        ip_destination  <= '0;
                        udp_source      <= '0;
                        udp_destination <= '0;
                        payload_length  <= '0;
                        reason          <= `FE_OK;
                    end else if (captured == 16'd8)
                        mac_source[31:0] <= net_mac[63:32];
                    if (mac_last) begin
                        wire_length <= capture_oversize ? FRAME_LIMIT[15:0] : capture_end[15:0];
                        state <= START;
                    end
                end
                START: begin
                    if (capture_bad)
                        reject(`FE_BAD_AXIS);
                    else if (capture_mac_error)
                        reject(`FE_MAC_BAD);
                    else if (capture_oversize)
                        reject(`FE_FRAME_LIMIT);
                    else if (wire_length < 16'd14)
                        reject(`FE_L2_SHORT);
                    else begin
                        l2_cursor <= 16'd12;
                        state <= L2_REQUEST;
                    end
                end
                L2_REQUEST: state <= L2_VALUE;
                L2_VALUE: begin
                    if (l2_type == 16'h8100 || l2_type == 16'h88a8) begin
                        if (vlan_count == 2)
                            reject(`FE_VLAN_LIMIT);
                        else if (wire_length < l2_cursor + 16'd6)
                            reject(`FE_L2_SHORT);
                        else begin
                            if (vlan_count == 0)
                                outer_tci <= l2_tci;
                            else
                                inner_tci <= l2_tci;
                            vlan_count <= vlan_count + 1'b1;
                            l2_cursor <= l2_cursor + 16'd4;
                            state <= L2_REQUEST;
                        end
                    end else if (l2_type != 16'h0800) begin
                        reject(`FE_L2_TYPE);
                    end else begin
                        ip_start <= l2_cursor + 16'd2;
                        if (wire_length < l2_cursor + 16'd22)
                            reject(`FE_IP_LENGTH);
                        else
                            state <= IP0_REQUEST;
                    end
                end
                IP0_REQUEST: state <= IP0_VALUE;
                IP0_VALUE: begin
                    ip_ihl   <= net_word[59:56];
                    ip_total <= net_word[47:32];
                    ip_ident <= net_word[31:16];
                    df       <= net_word[14];
                    if (net_word[63:60] != 4'd4 || net_word[59:56] < 4'd5)
                        reject(`FE_IP_SHAPE);
                    else if (net_word[47:32] < current_ipv4_bytes)
                        reject(`FE_IP_LENGTH);
                    else if (net_word[15] || net_word[13:0] != 0)
                        reject(`FE_IP_FRAGMENT);
                    else
                        state <= IP1_REQUEST;
                end
                IP1_REQUEST: state <= IP1_VALUE;
                IP1_VALUE: begin
                    ip_ttl      <= net_word[63:56];
                    ip_protocol <= net_word[55:48];
                    ip_source   <= net_word[31:0];
                    if (net_word[63:56] == 0)
                        reject(`FE_IP_SHAPE);
                    else if ({1'b0,wire_length} < ip_end || wire_length > minimum_frame)
                        reject(`FE_IP_LENGTH);
                    else
                        state <= IP2_REQUEST;
                end
                IP2_REQUEST: state <= IP2_VALUE;
                IP2_VALUE: begin
                    ip_destination <= net_word[63:32];
                    scan_position  <= ip_start;
                    remaining      <= ipv4_header_bytes;
                    accumulator    <= 32'd0;
                    udp_start      <= ip_start + ipv4_header_bytes;
                    state          <= IP_SUM_FIRST;
                end
                IP_SUM_FIRST: state <= IP_SUM_NEXT;
                IP_SUM_NEXT: begin
                    if (remaining > 16'd8) begin
                        accumulator   <= summed;
                        remaining     <= remaining - 16'd8;
                        scan_position <= scan_position + 16'd8;
                    end else if (fold_sum(summed) != 16'hffff) begin
                        reject(`FE_IP_CHECKSUM);
                    end else if (ip_protocol != 8'd17) begin
                        reject(`FE_IP_PROTOCOL);
                    end else if (ip_total < ipv4_header_bytes + 16'd8) begin
                        reject(`FE_UDP_LENGTH);
                    end else begin
                        state <= UDP_REQUEST;
                    end
                end
                UDP_REQUEST: state <= UDP_VALUE;
                UDP_VALUE: begin
                    udp_source      <= net_word[63:48];
                    udp_destination <= net_word[47:32];
                    udp_length      <= net_word[31:16];
                    udp_zero        <= net_word[15:0] == 0;
                    if (net_word[31:16] < 16'd8 || net_word[31:16] != ip_total-ipv4_header_bytes)
                        reject(`FE_UDP_LENGTH);
                    else begin
                        payload_start  <= udp_start + 16'd8;
                        payload_length <= net_word[31:16] - 16'd8;
                        scan_position  <= udp_start;
                        remaining      <= net_word[31:16];
                        accumulator    <= pseudo_sum(ip_source, ip_destination, net_word[31:16]);
                        if (net_word[15:0] != 0)
                            state <= UDP_SUM_FIRST;
                        else if (ALLOW_ZERO_UDP)
                            state <= OFFER;
                        else
                            reject(`FE_UDP_ZERO);
                    end
                end
                UDP_SUM_FIRST: state <= UDP_SUM_NEXT;
                UDP_SUM_NEXT: begin
                    if (remaining > 16'd8) begin
                        accumulator   <= summed;
                        remaining     <= remaining - 16'd8;
                        scan_position <= scan_position + 16'd8;
                    end else if (fold_sum(summed) != 16'hffff) begin
                        reject(`FE_UDP_CHECKSUM);
                    end else begin
                        state <= OFFER;
                    end
                end
                OFFER: if (inspect_take) begin
                    if (inspect_allow && payload_length != 0) begin
                        scan_position <= payload_start;
                        remaining     <= udp_length - 16'd8;
                        state         <= PAYLOAD_REQUEST;
                    end else begin
                        recycle();
                    end
                end
                PAYLOAD_REQUEST: state <= PAYLOAD_VALUE;
                PAYLOAD_VALUE: if (finish_payload) begin
                    recycle();
                end else if (m_valid && m_ready) begin
                    scan_position <= scan_position + 16'd8;
                    remaining <= remaining - 16'd8;
                end
                default: recycle();
            endcase
        end
    end
endmodule
