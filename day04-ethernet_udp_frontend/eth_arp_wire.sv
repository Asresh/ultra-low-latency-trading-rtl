`timescale 1ns/1ps
`default_nettype none

// One ARP MAC-client port. Upstream routes ARP frames here and sends IPv4 to
// the existing frontend. Network bytes are mapped to low lanes first.
module eth_arp_wire #(
    parameter logic [47:0] LOCAL_MAC = 48'h0200_0000_000a,
    parameter logic [31:0] LOCAL_IP = 32'hc000_020a,
    parameter integer MAX_FRAME_BYTES = 1536
) (
    input wire clk,
    input wire rst,
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

    input wire s_probe_valid,
    output wire s_probe_ready,
    input wire s_probe_tagged,
    input wire [11:0] s_probe_vid,
    input wire [31:0] s_probe_ip,
    output logic m_reply_valid,
    input wire m_reply_ready,
    output logic m_reply_tagged,
    output logic [11:0] m_reply_vid,
    output logic [31:0] m_reply_ip,
    output logic [47:0] m_reply_mac,
    output logic [31:0] rx_rejected,
    output logic [31:0] rx_replies,
    output logic [31:0] tx_answers
);
    localparam logic [47:0] BROADCAST = 48'hffff_ffff_ffff;
    localparam logic [15:0] ARP_TYPE = 16'h0806, VLAN_TYPE = 16'h8100;
    localparam logic [15:0] REQUEST = 16'd1, REPLY = 16'd2;
    logic [367:0] rx_header;
    logic [15:0] rx_length;
    logic [2:0] rx_word;
    logic rx_pending, rx_open, rx_bad;
    logic answer_valid, answer_tagged;
    logic [15:0] answer_tci;
    logic [47:0] answer_mac;
    logic [31:0] answer_ip;
    logic prefer_answer;
    logic [479:0] tx_frame;
    logic tx_busy;
    logic tx_is_answer;
    logic [2:0] tx_word;

    wire frame_tagged = rx_header[271:256] == VLAN_TYPE;
    wire [15:0] tci = frame_tagged ? rx_header[255:240] : 16'd0;
    wire [15:0] ether_type = frame_tagged ? rx_header[239:224] : rx_header[271:256];
    wire [47:0] ethernet_dst = rx_header[367:320];
    wire [47:0] ethernet_src = rx_header[319:272];
    wire [223:0] arp = frame_tagged ? rx_header[223:0] : rx_header[255:32];
    wire [15:0] opcode = arp[175:160];
    wire [47:0] sender_mac = arp[159:112];
    wire [31:0] sender_ip = arp[111:80];
    wire [47:0] target_mac = arp[79:32];
    wire [31:0] target_ip = arp[31:0];
    wire header_ok = !rx_bad && int'(rx_length) <= MAX_FRAME_BYTES &&
                     rx_length >= (frame_tagged ? 16'd46 : 16'd42) &&
                     ether_type == ARP_TYPE && tci[11:0] != 12'hfff &&
                     arp[223:176] == 48'h0001_0800_0604 &&
                     sender_mac == ethernet_src && sender_mac != 0 && !sender_mac[40] &&
                     target_ip == LOCAL_IP && sender_ip != LOCAL_IP &&
                     sender_ip[31:28] < 4'd14;
    wire wants_reply = header_ok && opcode == REQUEST &&
                       (ethernet_dst == LOCAL_MAC || ethernet_dst == BROADCAST);
    wire can_learn = header_ok && opcode == REPLY &&
                     ethernet_dst == LOCAL_MAC && target_mac == LOCAL_MAC &&
                     sender_ip[31:24] != 0 && sender_ip[31:24] != 8'd127;
    wire reply_room = !m_reply_valid || m_reply_ready;
    wire choose_answer = answer_valid && (!s_probe_valid || prefer_answer);
    wire start_answer = !rst && !tx_busy && choose_answer;
    wire start_probe = !rst && !tx_busy && !choose_answer && s_probe_valid;
    wire answer_room = !answer_valid || start_answer;
    wire parse_ready = (!wants_reply || answer_room) && (!can_learn || reply_room);
    assign s_mac_ready = !rst && !rx_pending;
    wire rx_transfer = s_mac_valid && s_mac_ready;
    wire tx_transfer = m_mac_valid && m_mac_ready;
    // Acknowledge the resolver's probe only when it has reached the MAC.
    // An indefinitely stalled transmit stream therefore cannot spend retries.
    assign s_probe_ready = tx_transfer && m_mac_last && !tx_is_answer;
    wire [2:0] capture_word = rx_open ? rx_word : 3'd0;
    wire keep_ok = s_mac_keep != 0 && ((s_mac_keep & (s_mac_keep+1'b1)) == 0) &&
                   (s_mac_last || s_mac_keep == 8'hff);

    function automatic logic [31:0] inc_sat(input logic [31:0] value);
        inc_sat = (&value) ? value : value + 1'b1;
    endfunction

    function automatic logic [15:0] add_lanes(input logic [15:0] previous, input logic [7:0] keep);
        logic [16:0] next_length;
        begin
            next_length = {1'b0,previous};
            for (integer lane = 0; lane < 8; lane++)
                next_length = next_length + 17'(keep[lane]);
            add_lanes = next_length[16] ? '1 : next_length[15:0];
        end
    endfunction

    function automatic logic [479:0] emit_frame(
        input logic is_answer,
        input logic vlan,
        input logic [15:0] vlan_tci,
        input logic [47:0] mac,
        input logic [31:0] ip
    );
        logic [223:0] payload;
        logic [47:0] destination;
        begin
            destination = is_answer ? mac : BROADCAST;
            payload = {48'h0001_0800_0604, is_answer ? REPLY : REQUEST,
                       LOCAL_MAC, LOCAL_IP, is_answer ? mac : 48'd0, ip};
            emit_frame = vlan
                ? {destination, LOCAL_MAC, VLAN_TYPE, vlan_tci, ARP_TYPE, payload, 112'd0}
                : {destination, LOCAL_MAC, ARP_TYPE, payload, 144'd0};
        end
    endfunction

    assign m_mac_valid = !rst && tx_busy;
    assign m_mac_first = m_mac_valid && tx_word == 0;
    assign m_mac_last = m_mac_valid && tx_word == 7;
    assign m_mac_keep = m_mac_valid ? (m_mac_last ? 8'h0f : 8'hff) : 8'h00;
    for (genvar lane = 0; lane < 8; lane = lane + 1) begin : g_tx_lane
        assign m_mac_data[8*lane+:8] = m_mac_keep[lane]
            ? tx_frame[479-64*int'(tx_word)-8*lane-:8] : 8'd0;
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            rx_header <= '0;
            rx_pending <= 1'b0;
            rx_open <= 1'b0;
            rx_bad <= 1'b0;
            rx_length <= '0;
            rx_word <= '0;
            m_reply_valid <= 1'b0;
            m_reply_tagged <= 1'b0;
            m_reply_vid <= '0;
            m_reply_mac <= '0;
            m_reply_ip <= '0;
            answer_valid <= 1'b0;
            answer_tagged <= 1'b0;
            answer_tci <= '0;
            answer_mac <= '0;
            answer_ip <= '0;
            rx_rejected <= '0;
            rx_replies <= '0;
        end else begin
            if (m_reply_ready)
                m_reply_valid <= 1'b0;
            if (start_answer)
                answer_valid <= 1'b0;
            if (rx_transfer) begin
                if (!rx_open)
                    rx_header <= '0;
                for (integer lane = 0; lane < 8; lane++)
                    if (s_mac_keep[lane] && 8*int'(capture_word)+lane < 46)
                        rx_header[367-64*int'(capture_word)-8*lane-:8] <= s_mac_data[8*lane+:8];
                rx_word <= capture_word < 6 ? capture_word + 1'b1 : capture_word;
                rx_length <= add_lanes(rx_open ? rx_length : 16'd0, s_mac_keep);
                rx_bad <= (rx_open && rx_bad) || s_mac_bad || !keep_ok ||
                          (s_mac_first == rx_open);
                rx_open <= !s_mac_last;
                rx_pending <= s_mac_last;
            end
            if (rx_pending && parse_ready) begin
                rx_pending <= 1'b0;
                if (wants_reply) begin
                    answer_valid <= 1'b1;
                    answer_tagged <= frame_tagged;
                    answer_tci <= tci;
                    answer_mac <= sender_mac;
                    answer_ip <= sender_ip;
                end else if (can_learn) begin
                    m_reply_valid <= 1'b1;
                    m_reply_tagged <= frame_tagged;
                    m_reply_vid <= tci[11:0];
                    m_reply_mac <= sender_mac;
                    m_reply_ip <= sender_ip;
                    rx_replies <= inc_sat(rx_replies);
                end else begin
                    rx_rejected <= inc_sat(rx_rejected);
                end
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            tx_frame <= '0;
            tx_busy <= 1'b0;
            tx_is_answer <= 1'b0;
            tx_word <= '0;
            tx_answers <= '0;
            prefer_answer <= 1'b1;
        end else begin
            if (tx_transfer) begin
                if (m_mac_last) begin
                    tx_busy <= 1'b0;
                    if (tx_is_answer)
                        tx_answers <= inc_sat(tx_answers);
                end else begin
                    tx_word <= tx_word + 1'b1;
                end
            end
            if (start_answer || start_probe) begin
                tx_frame <= start_answer
                    ? emit_frame(1'b1, answer_tagged, answer_tci, answer_mac, answer_ip)
                    : emit_frame(1'b0, s_probe_tagged, {4'd0,s_probe_vid}, 48'd0, s_probe_ip);
                tx_busy <= 1'b1;
                tx_is_answer <= start_answer;
                tx_word <= '0;
                prefer_answer <= !start_answer;
            end
        end
    end
endmodule

`default_nettype wire
