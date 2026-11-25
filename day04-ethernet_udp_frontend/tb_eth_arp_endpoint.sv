`timescale 1ns/1ps
`default_nettype none

module tb_eth_arp_endpoint;
    localparam logic [47:0] LOCAL_MAC = 48'h0200_0000_000a;
    localparam logic [31:0] LOCAL_IP = 32'hc000_020a;
    localparam logic [47:0] PEER_MAC = 48'h0200_0000_0014;
    localparam logic [31:0] PEER_IP = 32'hc000_0214;
    localparam logic [31:0] NEXT_IP = 32'hc000_021e;
    localparam logic [31:0] THIRD_IP = 32'hc000_0228;
    localparam logic [47:0] BROADCAST = '1;
    logic clk = 1'b0;
    always #5 clk = !clk;
    logic rst = 1;
    logic s_valid = 0, s_tagged = 0, m_ready = 0;
    wire s_ready, m_valid;
    logic [11:0] s_vid = 0;
    logic [31:0] s_ip = 0;
    logic [15:0] s_cookie = 0;
    wire [2:0] m_status;
    wire [15:0] m_cookie;
    wire [47:0] m_mac;
    logic s_mac_valid = 0, s_mac_first = 0, s_mac_last = 0, s_mac_bad = 0;
    wire s_mac_ready;
    logic [63:0] s_mac_data = 0;
    logic [7:0] s_mac_keep = 0;
    wire m_mac_valid, m_mac_first, m_mac_last;
    logic m_mac_ready = 0, allow_tx = 0, stall_pattern = 0;
    wire [63:0] m_mac_data;
    wire [7:0] m_mac_keep;
    wire [31:0] cache_hits, misses, joined, probes_sent, replies_learned, replies_ignored, timeouts;
    wire [31:0] rx_rejected, rx_replies, tx_answers;

    eth_arp_endpoint #(
        .CACHE_DEPTH(1), .PENDING_DEPTH(2), .WAITERS(2), .CACHE_TTL(112),
        .RETRY_CYCLES(48), .MAX_ATTEMPTS(2)
    ) dut (.*);

    logic [479:0] monitor_frame = 0;
    integer capture, tx_words = 0, frames = 0, tx_stalls = 0;
    integer cycle = 0, response_stalls = 0;
    logic held_mac = 0, held_response = 0;
    logic [73:0] previous_mac;
    logic [66:0] previous_response;
    string path;

    always @(negedge clk) begin
        #1;
        m_mac_ready = allow_tx && (!stall_pattern || cycle % 5 != 3);
    end

    always @(posedge clk) begin
        cycle++;
        if (rst) begin
            held_mac = 0;
            held_response = 0;
            tx_words = 0;
        end else begin
            if (held_mac && (!m_mac_valid ||
                previous_mac !== {m_mac_data,m_mac_keep,m_mac_first,m_mac_last}))
                $fatal(1, "MAC output changed while stalled");
            if (held_response && (!m_valid || previous_response !== {m_status,m_cookie,m_mac}))
                $fatal(1, "Resolution output changed while stalled");
            held_mac = m_mac_valid && !m_mac_ready;
            held_response = m_valid && !m_ready;
            if (held_mac) tx_stalls++;
            if (held_response) response_stalls++;
            previous_mac = {m_mac_data,m_mac_keep,m_mac_first,m_mac_last};
            previous_response = {m_status,m_cookie,m_mac};
            if (m_mac_valid && m_mac_ready) begin
                if (m_mac_first != (tx_words == 0) || m_mac_last != (tx_words == 7) ||
                    m_mac_keep != (tx_words == 7 ? 8'h0f : 8'hff))
                    $fatal(1, "ARP TX framing is invalid at beat %0d", tx_words);
                for (integer lane = 0; lane < 8; lane++)
                    if (m_mac_keep[lane])
                        monitor_frame[479-64*tx_words-8*lane-:8] = m_mac_data[8*lane+:8];
                    else if (m_mac_data[8*lane+:8] !== 0)
                        $fatal(1, "Inactive TX lanes must be zero");
                tx_words++;
                if (m_mac_last) begin
                    $fdisplay(capture, "%0120h", monitor_frame);
                    tx_words = 0;
                    frames++;
                end
            end
        end
    end

    // Offer one lookup until the resolver has accepted the cookie.
    task automatic query(
        input logic vlan, input logic [11:0] vid, input logic [31:0] ip,
        input logic [15:0] cookie
    );
        @(negedge clk);
        {s_tagged,s_vid,s_ip,s_cookie} = {vlan,vid,ip,cookie};
        s_valid = 1;
        do @(posedge clk); while (!s_ready);
        @(negedge clk);
        s_valid = 0;
    endtask

    // Wait for the report, hold it on purpose, then transfer its exact tuple.
    task automatic result(
        input logic [15:0] cookie, input logic [2:0] status, input logic [47:0] mac
    );
        wait (m_valid);
        repeat (2) @(negedge clk);
        if ({m_cookie,m_status,m_mac} !== {cookie,status,mac})
            $fatal(1, "cookie %0d expected %0d/%h, observed %0d/%0d/%h",
                   cookie,status,mac,m_cookie,m_status,m_mac);
        m_ready = 1;
        @(negedge clk);
        m_ready = 0;
    endtask

    // Drive a network-order test frame with selected independent MAC faults.
    task automatic inbound(
        input logic vlan, input logic [15:0] vlan_tci, input logic [15:0] op,
        input logic [47:0] ethernet_dst, input logic [47:0] ethernet_src,
        input logic [47:0] arp_src, input logic [31:0] arp_ip,
        input logic [47:0] arp_dst, input logic [31:0] arp_target_ip,
        input integer bytes, input bit mark_bad, input bit gap_keep
    );
        logic [223:0] body;
        logic [479:0] packet;
        integer words, tail_bytes;
        begin
            body = {16'd1,16'h0800,8'd6,8'd4,op,arp_src,arp_ip,arp_dst,arp_target_ip};
            packet = vlan
                ? {ethernet_dst,ethernet_src,16'h8100,vlan_tci,16'h0806,body,112'd0}
                : {ethernet_dst,ethernet_src,16'h0806,body,144'd0};
            words = (bytes + 7)/8;
            tail_bytes = bytes - 8*(words-1);
            for (integer beat = 0; beat < words; beat++) begin
                @(negedge clk);
                s_mac_data = 0;
                for (integer lane = 0; lane < 8; lane++)
                    if (beat*8+lane < bytes)
                        s_mac_data[8*lane+:8] = packet[479-64*beat-8*lane-:8];
                s_mac_first = beat == 0;
                s_mac_last = beat == words-1;
                s_mac_keep = s_mac_last ? 8'((1<<tail_bytes)-1) : 8'hff;
                if (gap_keep && s_mac_last) s_mac_keep = 8'h0b;
                s_mac_bad = mark_bad && (beat == 2);
                s_mac_valid = 1;
                do @(posedge clk); while (!s_mac_ready);
                if (beat % 3 == 2) begin
                    @(negedge clk);
                    s_mac_valid = 0;
                end
            end
            @(negedge clk);
            s_mac_valid = 0;
            s_mac_bad = 0;
        end
    endtask

    task automatic peer_reply(input bit vlan, input logic [15:0] tci);
        inbound(vlan,tci,2,LOCAL_MAC,PEER_MAC,PEER_MAC,PEER_IP,LOCAL_MAC,LOCAL_IP,
                60,0,0);
    endtask

    task automatic wait_for_frames(input integer expected);
        wait (frames >= expected);
        @(negedge clk);
        if (frames != expected)
            $fatal(1, "Expected %0d TX frames, got %0d", expected, frames);
    endtask

    initial begin
        if (!$value$plusargs("capture=%s", path))
            $fatal(1, "Specify +capture=FILE");
        capture = $fopen(path,"w");
        if (!capture) $fatal(1, "Cannot write ARP TX capture");
        if ($value$plusargs("wave=%s", path)) begin
            $dumpfile(path);
            $dumpvars(1,tb_eth_arp_endpoint);
        end
        repeat (3) @(negedge clk);
        rst = 0;
        query(0,0,32'he0010203,1);
        result(1,3,0);

        query(1,42,PEER_IP,101);
        query(1,42,PEER_IP,102);
        query(1,42,PEER_IP,103);
        result(103,2,0);
        peer_reply(1,42);
        repeat (110) @(negedge clk);
        if (probes_sent != 0 || timeouts != 0 || replies_ignored != 1 ||
            joined != 1 || !m_mac_valid)
            $fatal(1, "A held pre-wire probe spent credit or accepted an unsolicited reply");
        allow_tx = 1;
        stall_pattern = 1;
        wait_for_frames(1);
        peer_reply(1,43);
        peer_reply(1,42);
        result(101,0,PEER_MAC);
        result(102,0,PEER_MAC);
        query(1,42,PEER_IP,104);
        result(104,0,PEER_MAC);
        if (cache_hits != 1 || replies_ignored != 2 || replies_learned != 1)
            $fatal(1, "Cache or VLAN-scoped learning failed");

        // One-entry cache eviction after resolving the same IP on an
        // independent VLAN. A nonzero untagged VID is normalized to zero.
        query(0,77,PEER_IP,105);
        wait_for_frames(2);
        peer_reply(0,0);
        result(105,0,PEER_MAC);
        query(0,3,PEER_IP,106);
        result(106,0,PEER_MAC);
        inbound(1,16'ha02a,1,BROADCAST,PEER_MAC,PEER_MAC,PEER_IP,0,LOCAL_IP,
                46,0,0);
        wait_for_frames(3);
        if (tx_answers != 1) $fatal(1, "Request was not answered");

        inbound(0,0,2,LOCAL_MAC,PEER_MAC,48'h0200_0000_0015,PEER_IP,LOCAL_MAC,LOCAL_IP,
                60,0,0);
        inbound(0,0,2,LOCAL_MAC,PEER_MAC,PEER_MAC,PEER_IP,LOCAL_MAC,LOCAL_IP,
                40,0,0);
        inbound(1,42,1,BROADCAST,PEER_MAC,PEER_MAC,PEER_IP,0,LOCAL_IP,
                60,1,0);
        inbound(0,0,1,BROADCAST,PEER_MAC,PEER_MAC,PEER_IP,0,LOCAL_IP,
                60,0,1);
        repeat (116) @(negedge clk);
        if (rx_rejected != 4 || rx_replies != 4)
            $fatal(1, "Unexpected classified frame totals");

        // Expiration creates a new miss. A second unresolved neighbor can
        // start while the first waits for retries. A third yields BUSY.
        query(0,0,PEER_IP,201);
        wait_for_frames(4);
        query(1,12,NEXT_IP,202);
        query(0,0,THIRD_IP,203);
        result(203,2,0);
        wait_for_frames(5);
        result(201,1,0);
        result(202,1,0);
        wait_for_frames(7);
        if (timeouts != 2 || probes_sent != 6 || misses != 4 || tx_stalls < 110 ||
            response_stalls < 10)
            $fatal(1, "Probe, timeout, or stall coverage missing");

        query(0,0,THIRD_IP,210);
        wait_for_frames(8);
        allow_tx = 0;
        wait (m_mac_valid);
        inbound(0,0,2,LOCAL_MAC,PEER_MAC,PEER_MAC,THIRD_IP,LOCAL_MAC,LOCAL_IP,
                60,0,0);
        result(210,0,PEER_MAC);
        if (!m_mac_valid || frames != 8)
            $fatal(1, "A held redundant retry must not block a resolved cookie");
        allow_tx = 1;
        wait_for_frames(9);

        allow_tx = 0;
        query(0,0,NEXT_IP,211);
        wait (m_mac_valid);
        @(negedge clk);
        rst = 1;
        repeat (3) @(negedge clk);
        rst = 0;
        repeat (5) @(negedge clk);
        if (m_valid || m_mac_valid || probes_sent != 0)
            $fatal(1, "Reset did not discard incomplete resolution and frame");
        $fclose(capture);
        $display("PASS ARP endpoint: %0d wire frames, %0d MAC stalls, %0d result stalls",
                 frames,tx_stalls,response_stalls);
        $finish;
    end
    initial begin #40000; $fatal(1, "ARP simulation timeout"); end
endmodule

`default_nettype wire
