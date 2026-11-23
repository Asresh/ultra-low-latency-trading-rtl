`timescale 1ns/1ps

module tb_latency_capacity;
    localparam integer P = 3;
    localparam integer GROUP = 2;
    localparam integer TAG = 12;
    localparam integer RW = 65 + TAG + GROUP + P;
    localparam integer PAIRS = 2048;
    logic clk = 0;
    always #4 clk = !clk;
    logic reset = 1, flush = 0, armed = 1;
    logic [63:0] now_tick = 0;
    wire [63:0] timeout_ticks = 1024;
    logic s_valid = 0;
    wire s_ready;
    wire [RW-1:0] s_data;
    wire m_valid;
    logic m_ready = 1;
    wire [2:0] m_status;
    wire [TAG-1:0] m_tag;
    wire [GROUP-1:0] m_class;
    wire [P-1:0] m_start_probe, m_stop_probe;
    wire [63:0] m_start_tick,m_stop_tick,m_latency,m_observed_tick;
    wire [5:0] pending;
    wire [31:0] expired_on_touch;

    latency_correlator #(
        .TAG_W(TAG), .CLASS_W(GROUP), .PROBE_W(P), .INDEX_W(4), .SCAN_LOG(3)
    ) dut (.*);

    integer issued = 0, retired = 0, span = 0, maintenance = 0;
    logic [63:0] accepted_on [0:2*PAIRS-1];
    logic [63:0] first_cycle = '0, last_cycle = '0;
    wire [TAG-1:0] offer_tag = TAG'(issued >> 1);
    wire [GROUP-1:0] offer_group = GROUP'(offer_tag);
    wire offer_stop = 1'(issued);
    wire [63:0] timestamp = 64'(issued * 3 / 2);
    assign s_data = {offer_stop,offer_group,offer_tag,P'(offer_stop),timestamp};

    always @(posedge clk) begin
        now_tick <= now_tick + 1;
        if (reset) begin
            now_tick <= '0;
        end else begin
            if (s_valid && !s_ready)
                maintenance++;
            if (s_valid)
                span++;
            if (s_valid && s_ready) begin
                if (issued >= 2*PAIRS) $fatal(1, "source advanced beyond workload");
                if (issued == 0) first_cycle <= now_tick;
                accepted_on[issued] <= now_tick;
                issued <= issued + 1;
            end
            if (m_valid && m_ready) begin
                if (retired >= issued || m_tag !== TAG'(retired/2) ||
                    m_class !== GROUP'(retired/2))
                    $fatal(1, "Result displaced at record %0d", retired);
                if (now_tick - accepted_on[retired] != 1 || m_observed_tick !== accepted_on[retired])
                    $fatal(1, "Expected one-cycle unloaded response");
                if (retired % 2 == 0) begin
                    if (m_status != 0 || m_start_tick != 64'((retired * 3)/2) ||
                        {m_stop_tick,m_latency,m_stop_probe} != '0)
                        $fatal(1, "Start endpoint placement error");
                end else begin
                    if (m_status != 1 || m_start_tick != 64'(((retired-1)*3)/2) ||
                        m_stop_tick != 64'((retired*3)/2) || m_latency != 1 ||
                        m_start_probe != 0 || m_stop_probe != 1)
                        $fatal(1, "Incorrect measurement on a directed pair");
                end
                last_cycle <= now_tick;
                retired <= retired + 1;
            end
        end
    end

    initial begin
        repeat (5) @(negedge clk);
        reset = 0;
        repeat (PAIRS + 64) @(negedge clk);
        s_valid = 1;
        wait(issued == 2*PAIRS);
        @(negedge clk);
        s_valid = 0;
        wait (retired == 2*PAIRS);
        repeat (12) @(negedge clk);
        if (pending || expired_on_touch || m_valid || maintenance < (2*PAIRS/8))
            $fatal(1, "Clean workload left pending work or missed scan coverage");
        if (2*PAIRS != span-maintenance || span > 2*PAIRS + (2*PAIRS/7) + 2)
            $fatal(1, "Observed rate cannot account for one maintenance cycle per eight");
        $display("PASS capacity: %0d timestamps in %0d active edges, %0d scan bubbles; %0d full-window report cycles",
                 issued, span, maintenance, last_cycle-first_cycle);
        $finish;
    end
    initial begin #90000; $fatal(1, "Capacity timeout"); end
endmodule
