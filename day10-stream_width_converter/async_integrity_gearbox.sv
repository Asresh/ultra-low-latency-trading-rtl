`timescale 1ns/1ps
`default_nettype none

// Cross a stream into the width converter's clock domain and return its
// diagnostics on a separate Gray-pointer queue. A barrier shares ingress
// order with data but acknowledges only after older frames fully retire.
module async_integrity_gearbox #(
    parameter integer IN_BYTES=4,
    parameter integer OUT_BYTES=8,
    parameter integer MAX_FRAME=128,
    parameter integer IN_ADDR_W=3,
    parameter integer AUDIT_ADDR_W=3,
    parameter integer COUNT_W=32,
    localparam integer CMD_W=66+9*IN_BYTES,
    localparam integer AUDIT_W=180+2*COUNT_W
) (
    input  wire                   source_clk,
    input  wire                   dest_clk,
    input  wire                   power_reset,
    input  wire                   s_valid,
    output wire                   s_ready,
    input  wire [IN_BYTES*8-1:0]  s_data,
    input  wire [IN_BYTES-1:0]    s_keep,
    input  wire                   s_last,
    input  wire [15:0]            s_tag,
    input  wire [15:0]            s_expected_bytes,
    input  wire [31:0]            s_expected_crc,
    input  wire                   barrier_valid,
    output wire                   barrier_ready,
    input  wire [15:0]            barrier_tag,

    output wire                   m_valid,
    input  wire                   m_ready,
    output wire [OUT_BYTES*8-1:0] m_data,
    output wire [OUT_BYTES-1:0]   m_keep,
    output wire                   m_last,
    output wire [15:0]            m_tag,

    output wire                   audit_valid,
    input  wire                   audit_ready,
    output wire                   audit_barrier,
    output wire [15:0]            audit_tag,
    output wire [15:0]            audit_bytes,
    output wire [15:0]            audit_expected_bytes,
    output wire [31:0]            audit_crc,
    output wire [31:0]            audit_expected_crc,
    output wire [2:0]             audit_status,
    output wire [63:0]            audit_dest_tick,
    output wire [COUNT_W-1:0]     audit_retired_frames,
    output wire [COUNT_W-1:0]     audit_forwarded_frames
);
    wire source_reset, dest_reset;
    latency_reset_sync source_sync (.clk(source_clk), .power_reset, .reset(source_reset));
    latency_reset_sync dest_sync (.clk(dest_clk), .power_reset, .reset(dest_reset));

    logic open_packet;
    wire words_ready;
    wire takes_barrier = barrier_valid && !open_packet;
    wire barrier_commit = barrier_ready && barrier_valid;
    wire data_commit = s_valid && s_ready;
    wire [CMD_W-1:0] in_word = {
        takes_barrier,
        takes_barrier ? barrier_tag : s_tag,
        s_expected_bytes,s_expected_crc,s_last,s_keep,s_data
    };
    assign barrier_ready = !source_reset && !open_packet && words_ready;
    assign s_ready = !source_reset && !takes_barrier && words_ready;

    always_ff @(posedge source_clk or posedge source_reset)
        if (source_reset)
            open_packet <= 0;
        else if (data_commit)
            open_packet <= !s_last;

    wire read_valid, read_ready;
    wire [CMD_W-1:0] read_word;
    wire seen_barrier, seen_last;
    wire [15:0] seen_tag, seen_expected;
    wire [31:0] seen_crc;
    wire [IN_BYTES-1:0] seen_keep;
    wire [IN_BYTES*8-1:0] seen_data;
    assign {seen_barrier,seen_tag,seen_expected,seen_crc,seen_last,seen_keep,seen_data} = read_word;

    latency_async_fifo #(.WIDTH(CMD_W), .ADDRESS_W(IN_ADDR_W)) commands (
        .wclk(source_clk), .wreset(source_reset),
        .s_valid(barrier_commit || data_commit), .s_ready(words_ready), .s_data(in_word),
        .rclk(dest_clk), .rreset(dest_reset), .m_valid(read_valid), .m_ready(read_ready), .m_data(read_word)
    );

    wire engine_ready, diagnostic_valid, diagnostic_ready;
    wire [15:0] diagnostic_tag, diagnostic_bytes, diagnostic_expected;
    wire [31:0] diagnostic_crc, diagnostic_expected_crc;
    wire [2:0] diagnostic_status;
    integrity_gearbox #(.IN_BYTES(IN_BYTES), .OUT_BYTES(OUT_BYTES), .MAX_FRAME(MAX_FRAME)) gearbox (
        .clk(dest_clk), .rst(dest_reset),
        .s_valid(read_valid && !seen_barrier), .s_ready(engine_ready),
        .s_data(seen_data), .s_keep(seen_keep), .s_last(seen_last),
        .s_tag(seen_tag), .s_expected_bytes(seen_expected), .s_expected_crc(seen_crc),
        .m_valid, .m_ready, .m_data, .m_keep, .m_last, .m_tag,
        .audit_valid(diagnostic_valid), .audit_ready(diagnostic_ready), .audit_tag(diagnostic_tag),
        .audit_bytes(diagnostic_bytes), .audit_expected_bytes(diagnostic_expected),
        .audit_crc(diagnostic_crc), .audit_expected_crc(diagnostic_expected_crc),
        .audit_status(diagnostic_status)
    );

    // Counters are local modulo watermarks. The gearbox can only have a
    // bounded number of frames between these stages; no transfer across a
    // barrier is needed to determine the previous frame group has retired.
    localparam integer WATERMARK_W = COUNT_W < 4 ? 4 : COUNT_W;
    logic [WATERMARK_W-1:0] arrived, audited, approved, forwarded;
    logic [63:0] dest_tick;
    wire audit_fifo_ready;
    wire emit_diagnostic = diagnostic_valid && diagnostic_ready;
    wire settled = arrived == audited && approved == forwarded;
    wire emit_barrier = read_valid && seen_barrier && settled && !diagnostic_valid && audit_fifo_ready;
    wire take_final = read_valid && read_ready && !seen_barrier && seen_last;

    assign read_ready = seen_barrier ? emit_barrier : engine_ready;
    assign diagnostic_ready = audit_fifo_ready;
    wire [AUDIT_W-1:0] outgoing_report = emit_barrier ?
        {1'b1,seen_tag,16'd0,16'd0,32'd0,32'd0,3'd0,dest_tick,COUNT_W'(audited),COUNT_W'(forwarded)} :
        {1'b0,diagnostic_tag,diagnostic_bytes,diagnostic_expected,diagnostic_crc,
         diagnostic_expected_crc,diagnostic_status,dest_tick,COUNT_W'(0),COUNT_W'(0)};

    always_ff @(posedge dest_clk or posedge dest_reset) begin
        if (dest_reset) begin
            {arrived,audited,approved,forwarded,dest_tick} <= '0;
        end else begin
            dest_tick <= dest_tick+1'b1;
            if (take_final)
                arrived <= arrived+1'b1;
            if (emit_diagnostic) begin
                audited <= audited+1'b1;
                if (diagnostic_status == 0)
                    approved <= approved+1'b1;
            end
            if (m_valid && m_ready && m_last)
                forwarded <= forwarded+1'b1;
        end
    end

    wire [AUDIT_W-1:0] report_word;
    latency_async_fifo #(.WIDTH(AUDIT_W), .ADDRESS_W(AUDIT_ADDR_W)) reports (
        .wclk(dest_clk), .wreset(dest_reset), .s_valid(emit_barrier || emit_diagnostic),
        .s_ready(audit_fifo_ready), .s_data(outgoing_report),
        .rclk(source_clk), .rreset(source_reset), .m_valid(audit_valid), .m_ready(audit_ready),
        .m_data(report_word)
    );
    assign {audit_barrier,audit_tag,audit_bytes,audit_expected_bytes,audit_crc,audit_expected_crc,
            audit_status,audit_dest_tick,audit_retired_frames,audit_forwarded_frames} = report_word;
endmodule

`default_nettype wire
