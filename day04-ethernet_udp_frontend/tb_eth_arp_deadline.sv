`timescale 1ns/1ps

module tb_eth_arp_deadline;
    localparam logic [31:0] ADDRESS = 32'hc0000214;
    localparam logic [47:0] MAC = 48'h020000000014;
    localparam integer RETRY = 12;
    logic clk = 0;
    always #5 clk = !clk;
    logic rst = 1, s_valid = 0, m_ready = 1;
    logic s_tagged = 0;
    logic [11:0] s_vid = 0;
    logic [31:0] s_ip = ADDRESS;
    logic [15:0] s_cookie = 7;
    wire s_ready, m_valid;
    wire [15:0] m_cookie;
    wire [47:0] m_mac;
    wire [2:0] m_status;
    logic s_reply_valid = 0, s_reply_tagged = 0;
    logic [11:0] s_reply_vid = 0;
    logic [31:0] s_reply_ip = 0;
    logic [47:0] s_reply_mac = MAC;
    wire s_reply_ready, m_probe_valid;
    logic m_probe_ready = 1;
    wire m_probe_tagged;
    wire [11:0] m_probe_vid;
    wire [31:0] m_probe_ip;
    wire [31:0] cache_hits, misses, joined, probes_sent, replies_learned, replies_ignored, timeouts;

    eth_arp_resolver #(
        .CACHE_DEPTH(1), .PENDING_DEPTH(1), .WAITERS(1), .RETRY_CYCLES(RETRY), .MAX_ATTEMPTS(1)
    ) dut (.*);

    initial begin
        repeat (2) @(negedge clk);
        rst = 0;
        s_valid = 1;
        @(posedge clk);
        if (!s_ready) $fatal(1, "Initial lookup stalled");
        @(negedge clk);
        s_valid = 0;
        wait (probes_sent == 1);
        @(negedge clk);
        s_reply_valid = 1; // Unmatched reply toggles the basic round-robin priority.
        @(posedge clk);
        if (!s_reply_ready) $fatal(1, "Expected first reply to be consumed");
        @(negedge clk);
        s_reply_valid = 0;
        while (dut.tick - dut.pending_last_probe[0] != RETRY-1)
            @(negedge clk);
        s_reply_ip = ADDRESS;
        s_reply_valid = 1;
        s_ip = 32'hc0000215; // Competes for the table on the last acceptable edge.
        s_valid = 1;
        s_cookie = 8;
        @(posedge clk);
        if (!s_reply_ready || s_ready)
            $fatal(1, "A matching reply was delayed past the last acceptable edge");
        @(negedge clk);
        s_reply_valid = 0;
        do @(posedge clk); while (!s_ready);
        if (!m_valid || {m_cookie,m_status,m_mac} !== {16'd7,3'd0,MAC})
            $fatal(1, "The deadline-edge reply was not published before the next lookup");
        @(negedge clk);
        s_valid = 0;
        if (timeouts != 0 || replies_learned != 1 || replies_ignored != 1)
            $fatal(1, "Unexpected ARP deadline counters");
        $display("PASS matching ARP reply wins one edge before the watchdog expires");
        $finish;
    end
    initial begin #20000; $fatal(1,"ARP deadline test timeout"); end
endmodule
