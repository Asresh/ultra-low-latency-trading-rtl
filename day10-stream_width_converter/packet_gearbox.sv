`timescale 1ns/1ps
`default_nettype none

// An input stream commits only complete packets to one of two independent banks.
module packet_gearbox #(
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
    output wire                    m_valid,
    input  wire                    m_ready,
    output logic [OUT_BYTES*8-1:0] m_data,
    output logic [OUT_BYTES-1:0]   m_keep,
    output wire                    m_last,
    output wire [15:0]             m_tag,
    output logic                   report_valid,
    input  wire                    report_ready,
    output logic [15:0]            report_tag,
    output logic [15:0]            report_bytes,
    output logic [2:0]             report_status,
    output logic [31:0]            accepted_packets,
    output logic [31:0]            rejected_packets,
    output logic [31:0]            emitted_packets,
    output logic [31:0]            emitted_bytes
);
    localparam integer MAX_COUNT = MAX_FRAME + IN_BYTES > OUT_BYTES ? MAX_FRAME + IN_BYTES : OUT_BYTES;
    localparam integer COUNT_W = $clog2(MAX_COUNT + 1);
    localparam logic [2:0] OK = 3'd0, BAD_KEEP = 3'd1, BAD_TAG = 3'd2, TOO_LONG = 3'd3;
    logic [7:0] bank0 [0:MAX_FRAME-1];
    logic [7:0] bank1 [0:MAX_FRAME-1];
    logic [1:0] committed;
    logic [COUNT_W-1:0] bank_size [0:1];
    logic [15:0] bank_tag [0:1];
    logic writer, reader;
    logic open_packet;
    logic [2:0] sticky_error;
    logic [15:0] seen_bytes, captured_tag;
    logic [COUNT_W-1:0] written, rd_offset;

    // Count asserted lanes for reporting, including malformed masks. Lane n is byte n.
    function automatic logic [15:0] lane_count(input logic [IN_BYTES-1:0] mask);
        logic [15:0] amount;
        begin
            amount = 0;
            for (integer lane = 0; lane < IN_BYTES; lane++)
                amount = amount + 16'(mask[lane]);
            lane_count = amount;
        end
    endfunction

    // Count bytes until the 16-bit report field reaches its saturation value.
    function automatic logic [15:0] sat_bytes(input logic [15:0] count, delta);
        logic [16:0] sum;
        begin
            sum = {1'b0,count} + {1'b0,delta};
            sat_bytes = sum[16] ? '1 : sum[15:0];
        end
    endfunction

    // Update hardware statistics without silently wrapping on sustained traffic.
    function automatic logic [31:0] sat_counter(input logic [31:0] count, amount);
        logic [32:0] sum;
        begin
            sum = {1'b0,count} + {1'b0,amount};
            sat_counter = sum[32] ? '1 : sum[31:0];
        end
    endfunction

    wire [15:0] incoming_bytes = lane_count(s_keep);
    wire [COUNT_W-1:0] next_count = written + COUNT_W'(incoming_bytes);
    wire [15:0] packet_tag = open_packet ? captured_tag : s_tag;
    wire [15:0] report_sum = sat_bytes(seen_bytes, incoming_bytes);
    wire mask_is_prefix = s_keep != 0 && (s_keep & (s_keep + 1'b1)) == 0;
    wire input_fire = s_valid && s_ready;
    wire output_fire = m_valid && m_ready;
    wire [COUNT_W-1:0] remaining = bank_size[reader] - rd_offset;
    wire [COUNT_W-1:0] out_bytes =
        remaining < COUNT_W'(OUT_BYTES) ? remaining : COUNT_W'(OUT_BYTES);
    logic [2:0] error_now;

    assign s_ready = !rst && !committed[writer] &&
                     (open_packet || !report_valid || report_ready);
    assign m_valid = !rst && committed[reader];
    assign m_tag = m_valid ? bank_tag[reader] : '0;
    assign m_last = m_valid && remaining <= COUNT_W'(OUT_BYTES);

    always_comb begin
        error_now = sticky_error;
        if (sticky_error == OK) begin
            if (!mask_is_prefix || (!s_last && s_keep != '1))
                error_now = BAD_KEEP;
            else if (open_packet && s_tag != captured_tag)
                error_now = BAD_TAG;
            else if (next_count > COUNT_W'(MAX_FRAME))
                error_now = TOO_LONG;
        end
    end

    always_comb begin
        m_keep = '0;
        m_data = '0;
        for (integer lane = 0; lane < OUT_BYTES; lane++) begin
            if (m_valid && COUNT_W'(lane) < out_bytes) begin
                m_keep[lane] = 1;
                m_data[8*lane+:8] = reader ? bank1[int'(rd_offset)+lane] : bank0[int'(rd_offset)+lane];
            end
        end
    end

    always_ff @(posedge clk) begin
        if (input_fire && error_now == OK)
            for (integer lane = 0; lane < IN_BYTES; lane++)
                if (s_keep[lane]) begin
                    if (writer)
                        bank1[int'(written)+lane] <= s_data[8*lane+:8];
                    else
                        bank0[int'(written)+lane] <= s_data[8*lane+:8];
                end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            committed <= '0;
            writer <= 0;
            reader <= 0;
            open_packet <= 0;
            sticky_error <= OK;
            captured_tag <= '0;
            seen_bytes <= '0;
            written <= '0;
            rd_offset <= '0;
            report_valid <= 0;
            report_tag <= '0;
            report_bytes <= '0;
            report_status <= OK;
            accepted_packets <= '0;
            rejected_packets <= '0;
            emitted_packets <= '0;
            emitted_bytes <= '0;
        end else begin
            if (report_valid && report_ready)
                report_valid <= 0;
            if (input_fire) begin
                captured_tag <= packet_tag;
                if (s_last) begin
                    report_valid <= 1;
                    report_bytes <= report_sum;
                    report_tag <= packet_tag;
                    report_status <= error_now;
                    open_packet <= 0;
                    sticky_error <= OK;
                    written <= '0;
                    seen_bytes <= '0;
                    if (error_now == OK) begin
                        bank_size[writer] <= next_count;
                        bank_tag[writer] <= packet_tag;
                        committed[writer] <= 1;
                        writer <= !writer;
                        accepted_packets <= sat_counter(accepted_packets, 1);
                    end else begin
                        rejected_packets <= sat_counter(rejected_packets, 1);
                    end
                end else begin
                    open_packet <= 1;
                    sticky_error <= error_now;
                    seen_bytes <= report_sum;
                    if (error_now == OK)
                        written <= next_count;
                end
            end
            if (output_fire) begin
                emitted_bytes <= sat_counter(emitted_bytes, 32'(out_bytes));
                if (m_last) begin
                    committed[reader] <= 0;
                    reader <= !reader;
                    rd_offset <= '0;
                    emitted_packets <= sat_counter(emitted_packets, 1);
                end else begin
                    rd_offset <= rd_offset + out_bytes;
                end
            end
        end
    end
endmodule

`default_nettype wire
