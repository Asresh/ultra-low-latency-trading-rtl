`timescale 1ns/1ps
`default_nettype none

// Observe every driven cycle before its edge. The host model checks the complete
// ready/valid, data, diagnostic, occupancy, and counter trace independently.
module tb_multicast_packet_fanout #(
    parameter integer DATA_BYTES = 4,
    parameter integer PORTS = 3,
    parameter integer GROUPS = 4,
    parameter integer SLOTS = 4,
    parameter integer PORT_DEPTH = 2,
    parameter integer MAX_BEATS = 8,
    parameter integer TAG_W = 16,
    localparam integer GROUP_W = GROUPS > 1 ? $clog2(GROUPS+1) : 1,
    localparam integer DATA_W = 8 * DATA_BYTES,
    localparam integer COUNT_W = $clog2(PORT_DEPTH+1),
    localparam integer RESIDENT_W = $clog2(SLOTS+1)
);
    reg clk = 0;
    reg rst = 1;
    reg cfg_valid = 0;
    wire cfg_ready;
    reg [GROUP_W-1:0] cfg_group = 0;
    reg [PORTS-1:0] cfg_destinations = 0;
    reg cfg_drop_on_congestion = 0;
    reg s_valid = 0;
    wire s_ready;
    reg [DATA_W-1:0] s_data = 0;
    reg [DATA_BYTES-1:0] s_keep = 0;
    reg s_last = 0;
    reg [GROUP_W-1:0] s_group = 0;
    reg [TAG_W-1:0] s_tag = 0;
    wire [PORTS-1:0] m_valid;
    reg [PORTS-1:0] m_ready = 0;
    wire [PORTS*DATA_W-1:0] m_data;
    wire [PORTS*DATA_BYTES-1:0] m_keep;
    wire [PORTS-1:0] m_last;
    wire [PORTS*TAG_W-1:0] m_tag;
    wire report_valid;
    reg report_ready = 0;
    wire [2:0] report_status;
    wire [TAG_W-1:0] report_tag;
    wire [GROUP_W-1:0] report_group;
    wire [PORTS-1:0] report_destinations;
    wire [15:0] report_bytes;
    wire [31:0] forwarded_packets, filtered_packets, congested_packets, invalid_packets;
    wire [PORTS*32-1:0] delivered_packets;
    wire [PORTS*COUNT_W-1:0] port_occupancy;
    wire [RESIDENT_W-1:0] resident_slots;

    multicast_packet_fanout #(
        .DATA_BYTES(DATA_BYTES), .PORTS(PORTS), .GROUPS(GROUPS), .SLOTS(SLOTS),
        .PORT_DEPTH(PORT_DEPTH), .MAX_BEATS(MAX_BEATS), .TAG_W(TAG_W)
    ) dut (.*);

    integer stimulus_file, observed_file, result, cycle = 0;
    string stimulus_path, observed_path, waveform_path;

    initial begin
        if (!$value$plusargs("input=%s", stimulus_path) ||
            !$value$plusargs("output=%s", observed_path))
            $fatal(1, "Supply +input= and +output= paths");
        stimulus_file = $fopen(stimulus_path, "r");
        observed_file = $fopen(observed_path, "w");
        if (!stimulus_file || !observed_file)
            $fatal(1, "Cannot open the stimulus or observation path");
        if ($value$plusargs("waveform=%s", waveform_path)) begin
            $dumpfile(waveform_path);
            $dumpvars(1, tb_multicast_packet_fanout);
        end

        // Model starts from reset state. The actual RTL needs one reset edge.
        #5 clk = 1;
        #5 clk = 0;
        result = $fscanf(stimulus_file, "%h %h %h %h %h %h %h %h %h %h %h %h %h\n",
            rst, cfg_valid, cfg_group, cfg_destinations, cfg_drop_on_congestion,
            s_valid, s_data, s_keep, s_last, s_group, s_tag, m_ready, report_ready);
        while (result == 13) begin
            #5;
            $fdisplay(observed_file,
                "%0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h",
                cycle, cfg_ready, s_ready, m_valid, m_last, m_data, m_keep, m_tag,
                report_valid, report_valid ? report_status : 0, report_valid ? report_tag : 0,
                report_valid ? report_group : 0, report_valid ? report_destinations : 0,
                report_valid ? report_bytes : 0, forwarded_packets, filtered_packets,
                congested_packets, invalid_packets, delivered_packets, port_occupancy, resident_slots);
            clk = 1;
            #5 clk = 0;
            cycle = cycle + 1;
            result = $fscanf(stimulus_file, "%h %h %h %h %h %h %h %h %h %h %h %h %h\n",
                rst, cfg_valid, cfg_group, cfg_destinations, cfg_drop_on_congestion,
                s_valid, s_data, s_keep, s_last, s_group, s_tag, m_ready, report_ready);
        end
        if (result != -1)
            $fatal(1, "Malformed input row %0d: parsed %0d/13 fields", cycle, result);
        $fclose(stimulus_file);
        $fclose(observed_file);
        $display("Captured %0d cycles with %0d output ports", cycle, PORTS);
        $finish;
    end
endmodule

`default_nettype wire
