`timescale 1ns/1ps
`default_nettype none

// The two channels use separate banks, packet reports, and backpressure.
module stream_width_converter #(
    parameter integer MAX_FRAME = 128
) (
    input  wire        clk,
    input  wire        rst,
    input  wire        s32_valid,
    output wire        s32_ready,
    input  wire [31:0] s32_data,
    input  wire [3:0]  s32_keep,
    input  wire        s32_last,
    input  wire [15:0] s32_tag,
    output wire        m64_valid,
    input  wire        m64_ready,
    output wire [63:0] m64_data,
    output wire [7:0]  m64_keep,
    output wire        m64_last,
    output wire [15:0] m64_tag,
    output wire        up_report_valid,
    input  wire        up_report_ready,
    output wire [15:0] up_report_tag,
    output wire [15:0] up_report_bytes,
    output wire [2:0]  up_report_status,
    output wire [31:0] up_accepted,
    output wire [31:0] up_rejected,
    output wire [31:0] up_emitted,
    output wire [31:0] up_bytes,
    input  wire        s64_valid,
    output wire        s64_ready,
    input  wire [63:0] s64_data,
    input  wire [7:0]  s64_keep,
    input  wire        s64_last,
    input  wire [15:0] s64_tag,
    output wire        m32_valid,
    input  wire        m32_ready,
    output wire [31:0] m32_data,
    output wire [3:0]  m32_keep,
    output wire        m32_last,
    output wire [15:0] m32_tag,
    output wire        down_report_valid,
    input  wire        down_report_ready,
    output wire [15:0] down_report_tag,
    output wire [15:0] down_report_bytes,
    output wire [2:0]  down_report_status,
    output wire [31:0] down_accepted,
    output wire [31:0] down_rejected,
    output wire [31:0] down_emitted,
    output wire [31:0] down_bytes
);
    packet_gearbox #(.IN_BYTES(4),.OUT_BYTES(8),.MAX_FRAME(MAX_FRAME)) packer (
        .clk, .rst, .s_valid(s32_valid), .s_ready(s32_ready),
        .s_data(s32_data), .s_keep(s32_keep), .s_last(s32_last), .s_tag(s32_tag),
        .m_valid(m64_valid), .m_ready(m64_ready), .m_data(m64_data), .m_keep(m64_keep),
        .m_last(m64_last), .m_tag(m64_tag), .report_valid(up_report_valid),
        .report_ready(up_report_ready), .report_tag(up_report_tag), .report_bytes(up_report_bytes),
        .report_status(up_report_status), .accepted_packets(up_accepted), .rejected_packets(up_rejected),
        .emitted_packets(up_emitted), .emitted_bytes(up_bytes)
    );
    packet_gearbox #(.IN_BYTES(8),.OUT_BYTES(4),.MAX_FRAME(MAX_FRAME)) unpacker (
        .clk, .rst, .s_valid(s64_valid), .s_ready(s64_ready),
        .s_data(s64_data), .s_keep(s64_keep), .s_last(s64_last), .s_tag(s64_tag),
        .m_valid(m32_valid), .m_ready(m32_ready), .m_data(m32_data), .m_keep(m32_keep),
        .m_last(m32_last), .m_tag(m32_tag), .report_valid(down_report_valid),
        .report_ready(down_report_ready), .report_tag(down_report_tag), .report_bytes(down_report_bytes),
        .report_status(down_report_status), .accepted_packets(down_accepted), .rejected_packets(down_rejected),
        .emitted_packets(down_emitted), .emitted_bytes(down_bytes)
    );
endmodule

`default_nettype wire
