`timescale 1ns/1ps

module tb_latency_correlator #(
    parameter integer INDEX_W = 3,
    parameter integer SCAN_LOG = 3
);
    localparam integer TAG_W = 12;
    localparam integer CLASS_W = 2;
    localparam integer PROBE_W = 2;
    localparam integer VECTOR_W = 65 + TAG_W + CLASS_W + PROBE_W;
    localparam integer REPORT_W = 3 + TAG_W + CLASS_W + 2 * PROBE_W + 4 * 64;
    logic clk = 0;
    always #5 clk = !clk;

    logic reset, flush, armed;
    logic [63:0] now_tick, timeout_ticks;
    logic s_valid, m_ready;
    logic [VECTOR_W-1:0] s_data;
    wire s_ready, m_valid;
    wire [2:0] m_status;
    wire [TAG_W-1:0] m_tag;
    wire [CLASS_W-1:0] m_class;
    wire [PROBE_W-1:0] m_start_probe, m_stop_probe;
    wire [63:0] m_start_tick, m_stop_tick, m_latency, m_observed_tick;
    wire [INDEX_W+1:0] pending;
    wire [31:0] expired_on_touch;
    wire [REPORT_W-1:0] result = {
        m_status, m_class, m_tag, m_start_probe, m_stop_probe,
        m_start_tick, m_stop_tick, m_latency, m_observed_tick
    };

    latency_correlator #(
        .TAG_W(TAG_W), .CLASS_W(CLASS_W), .PROBE_W(PROBE_W),
        .INDEX_W(INDEX_W), .SCAN_LOG(SCAN_LOG)
    ) dut (.*);

    string filename;
    integer vectors;
    integer rc;
    integer cycles = 0;
    integer accepts = 0;
    integer replies = 0;
    integer blocked = 0;
    bit expect_ready, expect_valid;
    logic [REPORT_W-1:0] expected;
    logic [INDEX_W+1:0] expect_pending;
    logic [31:0] expect_expired;
    logic [7:0] seen = '0;
    initial begin
        if (!$value$plusargs("vectors=%s", filename)) $fatal(1, "Missing vector file");
        vectors = $fopen(filename, "r");
        if (vectors == 0) $fatal(1, "Cannot open %s", filename);
        reset = 1;
        flush = 0;
        armed = 0;
        s_valid = 0;
        s_data = 0;
        m_ready = 0;
        now_tick = 0;
        timeout_ticks = 1;
        repeat (2) @(negedge clk);
        rc = $fscanf(vectors, "%h %h %h %h %h %h %h %h %h %h %h %h %h",
                    reset, flush, armed, now_tick, timeout_ticks, s_valid, s_data, m_ready,
                    expect_ready, expect_valid, expected, expect_pending, expect_expired);
        while (rc == 13) begin
            #1;
            if (s_ready !== expect_ready || m_valid !== expect_valid ||
                pending !== expect_pending || expired_on_touch !== expect_expired)
                $fatal(1, "Control at cycle %0d: ready %b/%b valid %b/%b pending %0d/%0d expiration %0d/%0d",
                       cycles, s_ready, expect_ready, m_valid, expect_valid,
                       pending, expect_pending, expired_on_touch, expect_expired);
            if (m_valid && result !== expected)
                $fatal(1, "Payload at cycle %0d: got %h, expected %h", cycles, result, expected);
            if (s_valid && s_ready) accepts++;
            if (m_valid && !m_ready) blocked++;
            if (m_valid && m_ready && !flush) begin
                replies++;
                seen[m_status] = 1'b1;
            end
            cycles++;
            @(negedge clk);
            rc = $fscanf(vectors, "%h %h %h %h %h %h %h %h %h %h %h %h %h",
                        reset, flush, armed, now_tick, timeout_ticks, s_valid, s_data, m_ready,
                        expect_ready, expect_valid, expected, expect_pending, expect_expired);
        end
        if (!$feof(vectors) || cycles < 500 || replies == 0 || blocked == 0 || seen != 8'hff)
            $fatal(1, "Incomplete vector stream or incomplete status coverage at %0d", cycles);
        $display("PASS correlator: %0d cycles, %0d admitted, %0d replies, %0d stalls, statuses %h",
                 cycles, accepts, replies, blocked, seen);
        $fclose(vectors);
        $finish;
    end

    initial begin
        #2000000;
        $fatal(1, "Correlation regression timeout");
    end
endmodule
