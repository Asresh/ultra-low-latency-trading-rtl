`timescale 1ns/1ps
`default_nettype none

module tb_multicast_replay_buffer #(
    parameter integer PORTS=3, RETAIN=4, MAX_BEATS=8, DATA_BYTES=4, SEQUENCE_W=48,
                      EPOCH_W=16, TAG_W=16
);
    localparam integer PW = $clog2(PORTS+1), DW = DATA_BYTES*8;
    localparam integer HW = $clog2(RETAIN+1), RW = $clog2(RETAIN+PORTS+2);
    logic clk = 0, rst = 1;
    logic [EPOCH_W-1:0] reset_epoch = 0, cfg_epoch = 0;
    wire [EPOCH_W-1:0] epoch;
    logic cfg_valid = 0, cfg_report_ready = 0;
    logic [1:0] cfg_op = 0;
    logic [PW-1:0] cfg_port = 0;
    logic [SEQUENCE_W-1:0] cfg_sequence = 0, reset_sequence = 0;
    logic [TAG_W-1:0] cfg_tag = 0;
    wire cfg_ready, cfg_report_valid, cfg_report_enabled;
    wire [1:0] cfg_report_op;
    wire [2:0] cfg_report_status, report_status;
    wire [PW-1:0] cfg_report_port;
    wire [TAG_W-1:0] cfg_report_tag, report_tag;
    wire [SEQUENCE_W-1:0] cfg_report_cursor, report_sequence, earliest_sequence, next_sequence;
    logic s_valid = 0, s_last = 0, report_ready = 0;
    logic [DW-1:0] s_data = 0;
    logic [DATA_BYTES-1:0] s_keep = 0;
    logic [TAG_W-1:0] s_tag = 0;
    wire s_ready, report_valid, exhausted;
    wire [PORTS-1:0] m_valid,m_gap,m_first,m_last;
    logic [PORTS-1:0] m_ready = 0;
    wire [PORTS*SEQUENCE_W-1:0] m_sequence,m_resume,lost_packets;
    wire [PORTS*DW-1:0] m_data;
    wire [PORTS*DATA_BYTES-1:0] m_keep;
    wire [PORTS*TAG_W-1:0] m_tag;
    wire [PORTS*32-1:0] delivered_packets,gap_events;
    wire [HW-1:0] retained_packets;
    wire [RW-1:0] resident_slots;
    wire [SEQUENCE_W-1:0] p0_seq = m_sequence[0+:SEQUENCE_W];
    wire [SEQUENCE_W-1:0] p1_seq, p1_resume;
    if (PORTS > 1) begin : two_ports
        assign p1_seq = m_sequence[SEQUENCE_W+:SEQUENCE_W];
        assign p1_resume = m_resume[SEQUENCE_W+:SEQUENCE_W];
    end else begin : one_port
        assign p1_seq = '0;
        assign p1_resume = '0;
    end

    multicast_replay_buffer #(
        .PORTS(PORTS),.RETAIN(RETAIN),.MAX_BEATS(MAX_BEATS),.DATA_BYTES(DATA_BYTES),
        .SEQUENCE_W(SEQUENCE_W),.EPOCH_W(EPOCH_W),.TAG_W(TAG_W)
    ) dut (.*);

    integer stimulus, observed, parse_count, cycle = 0;
    string input_path, capture_path, wave_path;
    initial begin
        if (!$value$plusargs("input=%s",input_path) || !$value$plusargs("capture=%s",capture_path))
            $fatal(1,"input and capture paths are required");
        stimulus = $fopen(input_path,"r");
        observed = $fopen(capture_path,"w");
        if (!stimulus || !observed) $fatal(1,"could not open test artifacts");
        if ($value$plusargs("wave=%s",wave_path)) begin
            $dumpfile(wave_path);
            $dumpvars(1,tb_multicast_replay_buffer);
        end
        #5 clk = 1;
        #5 clk = 0;
        while (!$feof(stimulus)) begin
            parse_count = $fscanf(stimulus,"%h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h\n",
                rst,reset_epoch,reset_sequence,cfg_valid,cfg_op,cfg_port,cfg_epoch,cfg_sequence,cfg_tag,
                s_valid,s_data,s_keep,s_last,s_tag,m_ready,report_ready,cfg_report_ready);
            if (parse_count != 17) $fatal(1,"malformed stimulus at cycle %0d",cycle);
            #5;
            $fdisplay(observed,{"%0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h ",
                               "%0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h ",
                               "%0h %0h %0h %0h %0h %0h %0h %0h %0h"},
                cfg_ready,s_ready,m_valid,m_gap,m_first,m_last,m_sequence,m_resume,m_data,m_keep,m_tag,
                cfg_report_valid,cfg_report_valid ? cfg_report_status : 0,cfg_report_valid ? cfg_report_op : 0,
                cfg_report_valid ? cfg_report_port : 0,cfg_report_valid ? cfg_report_tag : 0,
                cfg_report_valid ? cfg_report_enabled : 0,cfg_report_valid ? cfg_report_cursor : 0,
                report_valid,report_valid ? report_status : 0,report_valid ? report_tag : 0,
                report_valid ? report_sequence : 0,epoch,next_sequence,earliest_sequence,retained_packets,
                resident_slots,exhausted,delivered_packets,gap_events,lost_packets);
            clk = 1;
            #5 clk = 0;
            cycle++;
        end
        $fclose(stimulus);
        $fclose(observed);
        $display("Captured %0d replay cycles with %0d ports and %0d retained packets",cycle,PORTS,RETAIN);
        $finish;
    end
endmodule

`default_nettype wire
