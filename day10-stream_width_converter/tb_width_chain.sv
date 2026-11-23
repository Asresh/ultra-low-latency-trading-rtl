`timescale 1ns/1ps
`default_nettype none

// Bridge each varying-width stage to the bench's uniform trace container.
// The physical handshake lanes remain the declared input and output widths.
module width_chain_element #(
    parameter integer IW = 4,
    parameter integer OW = 8,
    parameter integer DEPTH = 128,
    parameter integer STREAMING = 0
)(
    input  wire         clk,
    input  wire         rst,
    input  wire         s_valid,
    output wire         s_ready,
    input  wire [127:0] s_data,
    input  wire [15:0]  s_keep,
    input  wire         s_last,
    input  wire [15:0]  s_tag,
    output wire         m_valid,
    input  wire         m_ready,
    output wire [127:0] m_data,
    output wire [15:0]  m_keep,
    output wire         m_last,
    output wire [15:0]  m_tag,
    output wire         report_valid,
    input  wire         report_ready,
    output wire [15:0]  report_tag,
    output wire [15:0]  report_bytes,
    output wire [2:0]   report_status,
    output wire [15:0]  occupancy
);
    wire [8*OW-1:0] physical_data;
    wire [OW-1:0] physical_keep;
    assign m_data = 128'(physical_data);
    assign m_keep = 16'(physical_keep);

    if (STREAMING) begin : g_trusted
        wire [$clog2(DEPTH+1)-1:0] physical_occupancy;
        assign occupancy = 16'(physical_occupancy);
        assign {report_valid, report_tag, report_bytes, report_status} = '0;
        streaming_gearbox #(.IN_BYTES(IW), .OUT_BYTES(OW), .CAPACITY(DEPTH)) dut (
            .clk(clk), .rst(rst), .s_valid(s_valid), .s_ready(s_ready),
            .s_data(s_data[8*IW-1:0]), .s_keep(s_keep[IW-1:0]),
            .s_last(s_last), .s_tag(s_tag), .m_valid(m_valid), .m_ready(m_ready),
            .m_data(physical_data), .m_keep(physical_keep), .m_last(m_last),
            .m_tag(m_tag), .occupancy(physical_occupancy)
        );
    end else begin : g_validated
        assign occupancy = '0;
        packet_gearbox #(.IN_BYTES(IW), .OUT_BYTES(OW), .MAX_FRAME(DEPTH)) dut (
            .clk(clk), .rst(rst), .s_valid(s_valid), .s_ready(s_ready),
            .s_data(s_data[8*IW-1:0]), .s_keep(s_keep[IW-1:0]),
            .s_last(s_last), .s_tag(s_tag), .m_valid(m_valid), .m_ready(m_ready),
            .m_data(physical_data), .m_keep(physical_keep), .m_last(m_last),
            .m_tag(m_tag), .report_valid(report_valid), .report_ready(report_ready),
            .report_tag(report_tag), .report_bytes(report_bytes), .report_status(report_status),
            .accepted_packets(), .rejected_packets(), .emitted_packets(), .emitted_bytes()
        );
    end
endmodule

// The only stimulus is at the outside ports. Internal samples follow the
// connected RTL, exposing both packet conservation and backpressure.
module tb_width_chain #(
    parameter integer W0 = 4,
    parameter integer W1 = 8,
    parameter integer W2 = 3,
    parameter integer W3 = 4,
    parameter integer D0 = 128,
    parameter integer D1 = 17,
    parameter integer D2 = 128,
    parameter integer M0 = 0,
    parameter integer M1 = 1,
    parameter integer M2 = 0
);
    reg clk = 0;
    reg rst;
    reg in_valid, in_last, sink_ready;
    reg [127:0] in_data;
    reg [15:0] in_keep, in_tag;
    reg [2:0] d_ready;

    wire [3:0] link_valid, link_ready, link_last;
    wire [127:0] link_data [0:3];
    wire [15:0] link_keep [0:3];
    wire [15:0] link_tag [0:3];
    wire [2:0] d_valid;
    wire [15:0] d_tag [0:2];
    wire [15:0] d_bytes [0:2];
    wire [2:0] d_status [0:2];
    wire [15:0] occupancy [0:2];

    assign link_valid[0] = in_valid;
    assign link_data[0] = in_data;
    assign link_keep[0] = in_keep;
    assign link_last[0] = in_last;
    assign link_tag[0] = in_tag;
    assign link_ready[3] = sink_ready;

    for (genvar index = 0; index < 3; index++) begin : g_stage
        localparam integer IW = index == 0 ? W0 : index == 1 ? W1 : W2;
        localparam integer OW = index == 0 ? W1 : index == 1 ? W2 : W3;
        localparam integer DEPTH = index == 0 ? D0 : index == 1 ? D1 : D2;
        localparam integer STREAMING = index == 0 ? M0 : index == 1 ? M1 : M2;
        width_chain_element #(.IW(IW), .OW(OW), .DEPTH(DEPTH), .STREAMING(STREAMING)) stage (
            .clk(clk), .rst(rst),
            .s_valid(link_valid[index]), .s_ready(link_ready[index]), .s_data(link_data[index]),
            .s_keep(link_keep[index]), .s_last(link_last[index]), .s_tag(link_tag[index]),
            .m_valid(link_valid[index+1]), .m_ready(link_ready[index+1]),
            .m_data(link_data[index+1]), .m_keep(link_keep[index+1]),
            .m_last(link_last[index+1]), .m_tag(link_tag[index+1]),
            .report_valid(d_valid[index]), .report_ready(d_ready[index]), .report_tag(d_tag[index]),
            .report_bytes(d_bytes[index]), .report_status(d_status[index]), .occupancy(occupancy[index])
        );
    end

    // Sample all stage ports just before the clock edge. Output fields that
    // the protocol marks invalid are normalized to zero in the trace.
    task automatic sample_ports(input integer destination, input integer cycle);
        logic out_active, diag_active;
        begin
            $fwrite(destination, "%0h", cycle);
            for (integer i = 0; i < 3; i++) begin
                out_active = !rst && link_valid[i+1];
                diag_active = !rst && d_valid[i];
                $fwrite(destination, " %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h",
                    link_ready[i], out_active, out_active ? link_data[i+1] : 0,
                    out_active ? link_keep[i+1] : 0, out_active ? link_last[i+1] : 0,
                    out_active ? link_tag[i+1] : 0, diag_active, diag_active ? d_tag[i] : 0,
                    diag_active ? d_bytes[i] : 0, diag_active ? d_status[i] : 0,
                    rst ? 16'd0 : occupancy[i]);
            end
            $fwrite(destination, "\n");
        end
    endtask

    string driver_path, sample_path;
    integer driver, capture, cycle, fields;
    reg ready_0, ready_1, ready_2;
    initial begin
        if (!$value$plusargs("drive=%s", driver_path) || !$value$plusargs("sample=%s", sample_path))
            $fatal(1, "pass +drive= and +sample= output paths");
        driver = $fopen(driver_path, "r");
        capture = $fopen(sample_path, "w");
        if (driver == 0 || capture == 0)
            $fatal(1, "failed to open chain drive or sample");
        cycle = 0;
        while (!$feof(driver)) begin
            fields = $fscanf(driver, "%h %h %h %h %h %h %h %h %h %h\n",
                rst, in_valid, in_data, in_keep, in_last, in_tag, sink_ready,
                ready_0, ready_1, ready_2);
            if (fields == 10) begin
                if (cycle == 0 && !rst)
                    $fatal(1, "chain capture must begin with a reset edge");
                d_ready = {ready_2, ready_1, ready_0};
                #2;
                sample_ports(capture, cycle);
                clk = 1;
                #2;
                clk = 0;
                cycle++;
            end else if (!$feof(driver) || fields >= 0) begin
                $fatal(1, "malformed stimulus at edge %0d", cycle);
            end
        end
        if (cycle == 0)
            $fatal(1, "chain input tape is empty");
        $fclose(driver);
        $fclose(capture);
        $display("Sampled %0d hardware edges", cycle);
        $finish;
    end
endmodule

`default_nettype wire
