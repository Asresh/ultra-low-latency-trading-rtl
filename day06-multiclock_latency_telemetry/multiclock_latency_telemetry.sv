`timescale 1ns/1ps
`default_nettype none

module multiclock_latency_telemetry #(
    parameter integer PROBES = 3,
    parameter integer TAG_W = 16,
    parameter integer CLASS_W = 2,
    parameter integer FIFO_ADDRESS_W = 3,
    parameter integer TABLE_INDEX_W = 4,
    parameter integer SCAN_LOG = 3,
    parameter integer HIST_BINS = 24,
    parameter integer HIST_COUNTER_W = 32,
    parameter integer TRACE_ADDRESS_W = 4,
    parameter integer ADDRESS_W = 12,
    localparam integer PROBE_W = PROBES > 1 ? $clog2(PROBES) : 1,
    localparam integer RECORD_W = 65 + TAG_W + CLASS_W + PROBE_W,
    localparam integer CLASS_COUNT = 1 << CLASS_W,
    localparam integer PENDING_W = TABLE_INDEX_W + 2
) (
    input  wire                            ref_clk,
    input  wire                            power_reset,
    input  wire                            pps_async,
    input  wire [PROBES-1:0]              probe_clk,
    input  wire [PROBES-1:0]              probe_valid,
    output wire [PROBES-1:0]              probe_ready,
    input  wire [PROBES*TAG_W-1:0]        probe_tags,
    input  wire [PROBES*CLASS_W-1:0]      probe_classes,
    input  wire [PROBES-1:0]              probe_end,

    input  wire [ADDRESS_W-1:0]           s_awaddr,
    input  wire                           s_awvalid,
    output wire                           s_awready,
    input  wire [31:0]                    s_wdata,
    input  wire [3:0]                     s_wstrb,
    input  wire                           s_wvalid,
    output wire                           s_wready,
    output wire [1:0]                     s_bresp,
    output wire                           s_bvalid,
    input  wire                           s_bready,
    input  wire [ADDRESS_W-1:0]           s_araddr,
    input  wire                           s_arvalid,
    output wire                           s_arready,
    output wire [31:0]                    s_rdata,
    output wire [1:0]                     s_rresp,
    output wire                           s_rvalid,
    input  wire                           s_rready,

    output wire                           event_valid,
    input  wire                           event_ready,
    output wire [319:0]                   event_data,
    output wire                           hist_valid,
    input  wire                           hist_ready,
    output wire [127:0]                   hist_word,
    output wire                           hist_first,
    output wire                           hist_last,
    output wire                           trace_valid,
    input  wire                           trace_ready,
    output wire [319:0]                   trace_word,
    output wire                           trace_first,
    output wire                           trace_last,

    output wire                           pps_valid,
    input  wire                           pps_ready,
    output wire [63:0]                    pps_ticks,
    output wire [63:0]                    pps_nanoseconds,
    output wire [63:0]                    now_tick,
    output wire [63:0]                    now_ns,
    output wire                           epoch_exhausted
);
    localparam integer EPOCH_W = 8;
    localparam integer TAGGED_W = RECORD_W + EPOCH_W;
    wire ref_reset;
    wire [PROBES-1:0] local_reset;
    wire [63:0] gray_ticks;
    wire [31:0] now_fraction, pps_missed;
    wire rate_valid, rate_ready, saturated, invalid_rate;
    wire [31:0] rate_ns, rate_fraction;

    wire req_valid, req_ready, req_write;
    wire [ADDRESS_W-1:0] req_addr;
    wire [31:0] req_data, rsp_data;
    wire [3:0] req_strobe;
    wire rsp_valid, rsp_ready;
    wire [1:0] rsp_status;
    wire armed, flush, clear_counters;
    wire [63:0] timeout_ticks;
    wire trace_enable, trace_arm, trace_armed, trace_frozen;
    wire [TRACE_ADDRESS_W-1:0] trace_post;
    wire [63:0] trace_threshold;
    wire [7:0] trace_mask;
    wire hist_snapshot, hist_snapshot_ready, histogram_busy;
    wire [31:0] completed_snapshots, completed_traces;
    wire [31:0] expired_on_touch;
    wire [PENDING_W-1:0] pending;
    wire [255:0] statistics;

    logic [EPOCH_W-1:0] epoch;
    logic [EPOCH_W-1:0] epoch_gray;
    logic exhausted;
    wire [EPOCH_W-1:0] next_epoch = epoch + 1'b1;
    assign epoch_exhausted = exhausted;
    always_ff @(posedge ref_clk) begin
        if (ref_reset) begin
            epoch <= '0;
            epoch_gray <= '0;
            exhausted <= 1'b0;
        end else if (flush) begin
            if (epoch == '1)
                exhausted <= 1'b1;
            else begin
                epoch <= next_epoch;
                epoch_gray <= (next_epoch >> 1) ^ next_epoch;
            end
        end
    end

    latency_reset_sync reference_reset (
        .clk(ref_clk), .power_reset(power_reset), .reset(ref_reset)
    );
    latency_timebase timebase (
        .clk(ref_clk), .reset(ref_reset), .pps_async,
        .rate_valid, .rate_ready, .rate_ns, .rate_fraction,
        .ticks(now_tick), .gray_ticks,
        .nanoseconds(now_ns), .subnanoseconds(now_fraction),
        .saturated, .invalid_rate, .pps_valid, .pps_ready,
        .pps_ticks, .pps_nanoseconds, .pps_missed
    );

    wire [PROBES-1:0] crossed_valid, crossed_ready;
    wire [PROBES*TAGGED_W-1:0] crossed_data;
    for (genvar probe_no = 0; probe_no < PROBES; probe_no = probe_no + 1) begin : g_probe
        latency_reset_sync probe_reset (
            .clk(probe_clk[probe_no]), .power_reset(power_reset), .reset(local_reset[probe_no])
        );
        latency_probe #(
            .TAG_W(TAG_W), .CLASS_W(CLASS_W), .PROBE_W(PROBE_W),
            .PROBE_NUMBER(probe_no), .EPOCH_W(EPOCH_W), .FIFO_ADDRESS_W(FIFO_ADDRESS_W)
        ) probe (
            .clk(probe_clk[probe_no]), .reset(local_reset[probe_no]),
            .manager_clk(ref_clk), .manager_reset(ref_reset), .gray_ticks_async(gray_ticks),
            .epoch_gray_async(epoch_gray),
            .valid(probe_valid[probe_no]), .ready(probe_ready[probe_no]),
            .tag(probe_tags[probe_no*TAG_W +: TAG_W]),
            .event_class(probe_classes[probe_no*CLASS_W +: CLASS_W]),
            .end_event(probe_end[probe_no]),
            .manager_valid(crossed_valid[probe_no]), .manager_ready(crossed_ready[probe_no]),
            .observation(crossed_data[probe_no*TAGGED_W +: TAGGED_W])
        );
    end

    wire merge_valid, merge_ready;
    wire [TAGGED_W-1:0] merge_data;
    latency_rr #(.PORTS(PROBES), .WIDTH(TAGGED_W)) merge (
        .clk(ref_clk), .reset(ref_reset),
        .s_valid(crossed_valid), .s_ready(crossed_ready), .s_data(crossed_data),
        .m_valid(merge_valid), .m_ready(merge_ready), .m_data(merge_data)
    );

    wire report_valid;
    wire [2:0] report_status;
    wire [CLASS_W-1:0] report_class;
    wire [TAG_W-1:0] report_tag;
    wire [PROBE_W-1:0] report_start_probe, report_stop_probe;
    wire [63:0] report_start, report_stop, report_latency, report_tick;
    wire epoch_current = merge_data[TAGGED_W-1:RECORD_W] == epoch_gray;
    assign event_valid = report_valid && !flush && !ref_reset;
    assign event_data = {
        report_status, 8'(report_class), 16'(report_tag),
        8'(report_start_probe), 8'(report_stop_probe), 21'd0,
        report_start, report_stop, report_latency, report_tick
    };

    latency_correlator #(
        .TAG_W(TAG_W), .CLASS_W(CLASS_W), .PROBE_W(PROBE_W),
        .INDEX_W(TABLE_INDEX_W), .SCAN_LOG(SCAN_LOG)
    ) correlate (
        .clk(ref_clk), .reset(ref_reset), .flush,
        .armed(armed && !exhausted && epoch_current), .now_tick, .timeout_ticks,
        .s_valid(merge_valid), .s_ready(merge_ready), .s_data(merge_data[RECORD_W-1:0]),
        .m_valid(report_valid), .m_ready(event_ready),
        .m_status(report_status), .m_tag(report_tag), .m_class(report_class),
        .m_start_probe(report_start_probe), .m_stop_probe(report_stop_probe),
        .m_start_tick(report_start), .m_stop_tick(report_stop),
        .m_latency(report_latency), .m_observed_tick(report_tick), .pending, .expired_on_touch
    );

    wire report_transfer = event_valid && event_ready;
    latency_counter_bank counters (
        .clk(ref_clk), .reset(ref_reset), .clear(clear_counters),
        .accepted(report_transfer), .status(report_status), .statistics
    );
    latency_histogram #(
        .CLASSES(CLASS_COUNT), .BINS(HIST_BINS), .COUNTER_W(HIST_COUNTER_W)
    ) histogram (
        .clk(ref_clk), .reset(ref_reset),
        .sample_valid(report_transfer && report_status == 3'd1),
        .sample_class(report_class), .sample_latency(report_latency),
        .snapshot_valid(hist_snapshot), .snapshot_ready(hist_snapshot_ready),
        .snapshot_tick(now_tick), .m_valid(hist_valid), .m_ready(hist_ready),
        .m_word(hist_word), .m_first(hist_first), .m_last(hist_last),
        .completed(completed_snapshots), .busy(histogram_busy)
    );
    latency_trace #(.ADDRESS_W(TRACE_ADDRESS_W)) trace (
        .clk(ref_clk), .reset(ref_reset), .arm(trace_arm), .enable(trace_enable),
        .trigger_mask(trace_mask), .latency_threshold(trace_threshold), .post_count(trace_post),
        .sample_valid(report_transfer), .sample_status(report_status),
        .sample_latency(report_latency), .sample_tick(report_tick), .sample_data(event_data),
        .armed(trace_armed), .frozen(trace_frozen), .traces(completed_traces),
        .m_valid(trace_valid), .m_ready(trace_ready), .m_data(trace_word),
        .m_first(trace_first), .m_last(trace_last)
    );

    latency_axil #(.ADDRESS_W(ADDRESS_W)) axil (
        .clk(ref_clk), .reset(ref_reset),
        .s_awaddr, .s_awvalid, .s_awready, .s_wdata, .s_wstrb, .s_wvalid, .s_wready,
        .s_bresp, .s_bvalid, .s_bready, .s_araddr, .s_arvalid, .s_arready,
        .s_rdata, .s_rresp, .s_rvalid, .s_rready,
        .req_valid, .req_ready, .req_write, .req_addr, .req_data, .req_strobe,
        .rsp_valid, .rsp_ready, .rsp_data, .rsp_status
    );
    latency_registers #(
        .ADDRESS_W(ADDRESS_W), .PENDING_W(PENDING_W), .TRACE_ADDRESS_W(TRACE_ADDRESS_W),
        .PROBES(PROBES), .CLASS_COUNT(CLASS_COUNT)
    ) registers (
        .clk(ref_clk), .reset(ref_reset), .req_valid, .req_ready, .req_write,
        .req_addr, .req_data, .req_strobe, .rsp_valid, .rsp_ready, .rsp_data, .rsp_status,
        .now_tick, .now_ns, .now_fraction, .time_saturated(saturated), .invalid_rate,
        .pps_missed, .pending, .expired_touch(expired_on_touch), .statistics,
        .completed_snapshots, .completed_traces, .trace_armed, .trace_frozen, .histogram_busy,
        .armed, .flush, .clear_counters, .rate_valid, .rate_ready, .rate_ns, .rate_fraction,
        .timeout_ticks, .hist_snapshot, .hist_snapshot_ready, .trace_arm,
        .trace_enable, .trace_mask, .trace_threshold, .trace_post
    );
endmodule

`default_nettype wire
