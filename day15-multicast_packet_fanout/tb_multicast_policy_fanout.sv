`timescale 1ns/1ps
`default_nettype none

module tb_multicast_policy_fanout #(
    parameter integer DATA_BYTES = 4,
    parameter integer PORTS = 3,
    parameter integer GROUPS = 4,
    parameter integer SLOTS = 4,
    parameter integer PORT_DEPTH = 2,
    parameter integer MAX_BEATS = 8,
    parameter integer TAG_W = 16,
    parameter integer EPOCH_W = 3,
    localparam integer GROUP_W = $clog2(GROUPS+1),
    localparam integer DATA_W = 8 * DATA_BYTES,
    localparam integer COUNT_W = $clog2(PORT_DEPTH+1),
    localparam integer RESIDENT_W = $clog2(SLOTS+1)
);
    logic clk = 0;
    logic rst = 1;
    logic cfg_valid = 0, cfg_drop_on_congestion = 0;
    logic [GROUP_W-1:0] cfg_group = 0;
    logic [PORTS-1:0] cfg_requested = 0, cfg_mandatory = 0;
    logic [PORTS*COUNT_W-1:0] cfg_optional_marks = 0;
    wire cfg_ready, cfg_ack, cfg_ok;
    logic s_valid = 0, s_last = 0;
    wire s_ready;
    logic [DATA_W-1:0] s_data = 0;
    logic [DATA_BYTES-1:0] s_keep = 0;
    logic [GROUP_W-1:0] s_group = 0;
    logic [TAG_W-1:0] s_tag = 0;
    logic [PORTS-1:0] m_ready = 0;
    wire [PORTS-1:0] m_valid, m_last;
    wire [PORTS*DATA_W-1:0] m_data;
    wire [PORTS*DATA_BYTES-1:0] m_keep;
    wire [PORTS*TAG_W-1:0] m_tag;
    wire report_valid, report_no_slot;
    logic report_ready = 0;
    wire [2:0] report_status;
    wire [TAG_W-1:0] report_tag;
    wire [GROUP_W-1:0] report_group;
    wire [EPOCH_W-1:0] report_epoch;
    wire [PORTS-1:0] report_requested, report_mandatory, report_no_credit, report_enqueued;
    wire [15:0] report_bytes;
    wire [PORTS*COUNT_W-1:0] port_occupancy;
    wire [RESIDENT_W-1:0] resident_slots;
    wire [PORTS*32-1:0] delivered_packets, omitted_optional_packets;

    multicast_policy_fanout #(
        .DATA_BYTES(DATA_BYTES), .PORTS(PORTS), .GROUPS(GROUPS), .SLOTS(SLOTS),
        .PORT_DEPTH(PORT_DEPTH), .MAX_BEATS(MAX_BEATS), .TAG_W(TAG_W), .EPOCH_W(EPOCH_W)
    ) dut (.*);

    integer stimulus_file, observed_file, fields, cycle = 0;
    string stimulus_path, observed_path, waveform_path;

    initial begin
        if (!$value$plusargs("input=%s", stimulus_path) ||
            !$value$plusargs("output=%s", observed_path))
            $fatal(1, "Missing input or output path");
        stimulus_file = $fopen(stimulus_path, "r");
        observed_file = $fopen(observed_path, "w");
        if (!stimulus_file || !observed_file)
            $fatal(1, "Could not open the input or output file");
        if ($value$plusargs("waveform=%s", waveform_path)) begin
            $dumpfile(waveform_path);
            $dumpvars(1, tb_multicast_policy_fanout);
        end

        #5 clk = 1;
        #5 clk = 0;
        while (!$feof(stimulus_file)) begin
            fields = $fscanf(stimulus_file, "%h %h %h %h %h %h %h %h %h %h %h %h %h %h %h\n",
                rst, cfg_valid, cfg_group, cfg_requested, cfg_mandatory, cfg_optional_marks,
                cfg_drop_on_congestion, s_valid, s_data, s_keep, s_last, s_group,
                s_tag, m_ready, report_ready);
            if (fields != 15)
                $fatal(1, "Malformed input cycle %0d with %0d fields", cycle, fields);
            #5;
            $fdisplay(observed_file,
                "%0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h",
                cfg_ready, cfg_ack, cfg_ok, s_ready, m_valid, m_data, m_keep, m_last, m_tag, report_valid,
                report_valid ? report_status : 0, report_valid ? report_tag : 0, report_valid ? report_group : 0,
                report_valid ? report_epoch : 0, report_valid ? report_requested : 0,
                report_valid ? report_mandatory : 0, report_valid ? report_no_credit : 0,
                report_valid ? report_no_slot : 0, report_valid ? report_enqueued : 0,
                report_valid ? report_bytes : 0,
                port_occupancy, resident_slots, delivered_packets, omitted_optional_packets);
            clk = 1;
            #5 clk = 0;
            cycle++;
        end
        $fclose(stimulus_file);
        $fclose(observed_file);
        $display("Captured %0d policy cycles with %0d output ports", cycle, PORTS);
        $finish;
    end
endmodule

`default_nettype wire
