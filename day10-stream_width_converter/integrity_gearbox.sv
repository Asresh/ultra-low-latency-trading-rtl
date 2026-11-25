`timescale 1ns/1ps
`default_nettype none

// Check a declared byte length and CRC-32 before forwarding a repacked frame.
module integrity_gearbox #(
    parameter integer IN_BYTES = 4,
    parameter integer OUT_BYTES = 8,
    parameter integer MAX_FRAME = 128
) (
    input  wire                    clk,
    input  wire                    rst,
    input  wire                    s_valid,
    output wire                    s_ready,
    input  wire [IN_BYTES*8-1:0]   s_data,
    input  wire [IN_BYTES-1:0]     s_keep,
    input  wire                    s_last,
    input  wire [15:0]             s_tag,
    input  wire [15:0]             s_expected_bytes,
    input  wire [31:0]             s_expected_crc,
    output wire                    m_valid,
    input  wire                    m_ready,
    output wire [OUT_BYTES*8-1:0] m_data,
    output wire [OUT_BYTES-1:0]   m_keep,
    output wire                    m_last,
    output wire [15:0]             m_tag,
    output wire                    audit_valid,
    input  wire                    audit_ready,
    output logic [15:0]            audit_tag,
    output logic [15:0]            audit_bytes,
    output logic [15:0]            audit_expected_bytes,
    output logic [31:0]            audit_crc,
    output logic [31:0]            audit_expected_crc,
    output logic [2:0]             audit_status
);
    localparam logic [2:0] OK = 0, BAD_LENGTH = 4, BAD_CRC = 5;
    localparam logic [31:0] CRC_RESET = 32'hffffffff;
    logic open_packet, pending_audit;
    logic [31:0] crc_running, closed_crc, closed_expected_crc, packet_expected_crc;
    logic [15:0] packet_expected_bytes, closed_expected_bytes;

    // Exactly two underlying packet banks can contain framed data. The
    // decision queue has one bit per bank occupant whose report was consumed.
    // Malformed frames never occupy a bank and never enter this queue.
    logic [1:0] passed;
    logic read_pointer, write_pointer;
    logic [1:0] occupied;
    wire expect_data = occupied != 0;
    wire allow_data = expect_data && passed[read_pointer];
    wire raw_valid, raw_last, raw_audit_valid;
    wire [OUT_BYTES*8-1:0] raw_data;
    wire [OUT_BYTES-1:0] raw_keep;
    wire [15:0] raw_tag, raw_audit_tag, raw_audit_bytes;
    wire [2:0] raw_audit_status;
    wire raw_ready = expect_data && (!allow_data || m_ready);
    wire pop = raw_valid && raw_ready && raw_last;
    wire raw_audit_ready = !rst && (!pending_audit || audit_ready) &&
                           (raw_audit_status != OK || occupied < 2 || pop);
    wire take_report = raw_audit_valid && raw_audit_ready;
    wire push = take_report && raw_audit_status == OK;
    wire in_fire = s_valid && s_ready;
    logic [2:0] check_status;
    wire [31:0] crc_after = update_crc(crc_running, s_data, s_keep);
    wire [31:0] unused_accepted, unused_rejected, unused_packets, unused_bytes;

    // Reflected CRC-32/ISO-HDLC. Process selected lanes in increasing
    // order, including sparse masks so a rejected-frame report is repeatable.
    function automatic logic [31:0] update_crc(
        input logic [31:0] initial_crc,
        input logic [8*IN_BYTES-1:0] data,
        input logic [IN_BYTES-1:0] keep
    );
        logic [31:0] value;
        value = initial_crc;
        for (integer lane = 0; lane < IN_BYTES; lane = lane+1) begin
            if (keep[lane]) begin
                value = value ^ {24'd0,data[8*lane+:8]};
                for (integer bit_index = 0; bit_index < 8; bit_index = bit_index+1)
                    value = (value >> 1) ^ (value[0] ? 32'hedb88320 : 32'd0);
            end
        end
        update_crc = value;
    endfunction

    always_comb begin
        check_status = raw_audit_status;
        if (raw_audit_status == OK) begin
            if (raw_audit_bytes != closed_expected_bytes)
                check_status = BAD_LENGTH;
            else if (closed_crc != closed_expected_crc)
                check_status = BAD_CRC;
        end
    end

    assign audit_valid = !rst && pending_audit;
    assign m_valid = !rst && raw_valid && allow_data;
    assign m_data = m_valid ? raw_data : '0;
    assign m_keep = m_valid ? raw_keep : '0;
    assign m_last = m_valid && raw_last;
    assign m_tag = m_valid ? raw_tag : '0;

    packet_gearbox #(.IN_BYTES(IN_BYTES), .OUT_BYTES(OUT_BYTES), .MAX_FRAME(MAX_FRAME)) lanes (
        .clk, .rst, .s_valid, .s_ready, .s_data, .s_keep, .s_last, .s_tag,
        .m_valid(raw_valid), .m_ready(raw_ready), .m_data(raw_data), .m_keep(raw_keep),
        .m_last(raw_last), .m_tag(raw_tag), .report_valid(raw_audit_valid),
        .report_ready(raw_audit_ready), .report_tag(raw_audit_tag),
        .report_bytes(raw_audit_bytes), .report_status(raw_audit_status),
        .accepted_packets(unused_accepted), .rejected_packets(unused_rejected),
        .emitted_packets(unused_packets), .emitted_bytes(unused_bytes)
    );

    always_ff @(posedge clk) begin
        if (rst) begin
            open_packet <= 0;
            crc_running <= CRC_RESET;
            packet_expected_bytes <= '0;
            packet_expected_crc <= '0;
            closed_crc <= '0;
            closed_expected_bytes <= '0;
            closed_expected_crc <= '0;
        end else if (in_fire) begin
            if (!open_packet) begin
                packet_expected_bytes <= s_expected_bytes;
                packet_expected_crc <= s_expected_crc;
            end
            open_packet <= !s_last;
            if (s_last) begin
                crc_running <= CRC_RESET;
                closed_crc <= ~crc_after;
                closed_expected_bytes <= open_packet ? packet_expected_bytes : s_expected_bytes;
                closed_expected_crc <= open_packet ? packet_expected_crc : s_expected_crc;
            end else begin
                crc_running <= crc_after;
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            pending_audit <= 0;
            audit_tag <= '0;
            audit_bytes <= '0;
            audit_expected_bytes <= '0;
            audit_crc <= '0;
            audit_expected_crc <= '0;
            audit_status <= '0;
            occupied <= '0;
            passed <= '0;
            read_pointer <= 0;
            write_pointer <= 0;
        end else begin
            if (audit_ready)
                pending_audit <= 0;
            if (take_report) begin
                pending_audit <= 1;
                audit_tag <= raw_audit_tag;
                audit_bytes <= raw_audit_bytes;
                audit_expected_bytes <= closed_expected_bytes;
                audit_crc <= closed_crc;
                audit_expected_crc <= closed_expected_crc;
                audit_status <= check_status;
            end
            if (push) begin
                passed[write_pointer] <= check_status == OK;
                write_pointer <= !write_pointer;
            end
            if (pop)
                read_pointer <= !read_pointer;
            case ({push,pop})
                2'b10: occupied <= occupied + 1'b1;
                2'b01: occupied <= occupied - 1'b1;
                default: ;
            endcase
        end
    end
endmodule

`default_nettype wire
