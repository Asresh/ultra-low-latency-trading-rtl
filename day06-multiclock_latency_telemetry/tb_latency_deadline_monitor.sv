`timescale 1ns/1ps
`default_nettype none

module tb_latency_deadline_monitor #(
    parameter integer RULES = 3,
    parameter integer DEPTH = 3,
    parameter integer REV_W = 3
);
    localparam integer KEY_W = $clog2(RULES+1);
    localparam integer RULE_W = RULES > 1 ? $clog2(RULES) : 1;
    localparam integer OCC_W = $clog2(DEPTH+1);

    logic clk = 0, rst = 1, sample_valid = 0, ctl_valid = 0, alert_ready = 0, reply_ready = 0;
    logic [319:0] sample_data = '0;
    logic [1:0] ctl_op;
    logic [KEY_W-1:0] ctl_rule;
    logic [REV_W-1:0] ctl_expected_revision;
    logic ctl_enable;
    logic [7:0] ctl_class, ctl_start, ctl_stop;
    logic [2:0] ctl_wild;
    logic [63:0] ctl_limit;
    logic [15:0] ctl_trip, ctl_recover;
    wire ctl_ready, reply_valid, alert_valid;
    wire [2:0] reply_status;
    wire [1:0] reply_op;
    wire [KEY_W-1:0] reply_rule;
    wire [REV_W-1:0] reply_revision, alert_revision;
    wire [380:0] reply_data;
    wire [RULE_W-1:0] alert_rule;
    wire [255:0] alert_data;
    wire [OCC_W-1:0] alert_occupancy;
    wire [31:0] seen_reports, nonmatches, unclassified, lost_alerts;
    wire [7:0] alert_code = alert_data[215:208];
    wire [63:0] duration = sample_data[127:64];

    always #5 clk = ~clk;
    latency_deadline_monitor #(.RULES(RULES), .ALERT_DEPTH(DEPTH), .REV_W(REV_W)) dut (.*);

    integer vectors, capture, count, cycle = 0;
    integer reports = 0, replies = 0, alerts = 0, stalls = 0, drops = 0;
    logic [4:0] handshake;
    logic [125+KEY_W+REV_W:0] config_word;
    logic [2:0] expected_handshake;
    logic [385+KEY_W+REV_W:0] expected_reply;
    logic [255+RULE_W+REV_W:0] expected_alert;
    logic [OCC_W-1:0] expected_occupancy;
    logic [127:0] expected_stats;
    string path, waveform, capture_path;

    initial begin
        if (!$value$plusargs("vectors=%s",path))
            $fatal(1,"provide +vectors=PATH");
        vectors = $fopen(path,"r");
        if (!vectors)
            $fatal(1,"could not open vectors %s",path);
        capture = 0;
        if ($value$plusargs("capture=%s",capture_path)) begin
            capture = $fopen(capture_path,"w");
            if (!capture)
                $fatal(1,"could not open capture %s",capture_path);
        end
        if ($value$plusargs("deadline_waves=%s",waveform)) begin
            $dumpfile(waveform);
            $dumpvars(0,tb_latency_deadline_monitor);
        end
        @(posedge clk);
        while (!$feof(vectors)) begin
            @(negedge clk);
            count = $fscanf(vectors,"%h %h %h %h %h %h %h %h\n",
                handshake,sample_data,config_word,expected_handshake,expected_reply,
                expected_alert,expected_occupancy,expected_stats);
            if (count != 8)
                $fatal(1,"bad vector record at cycle %0d (%0d fields)",cycle,count);
            {rst,sample_valid,ctl_valid,alert_ready,reply_ready} = handshake;
            {ctl_op,ctl_rule,ctl_expected_revision,ctl_enable,ctl_class,ctl_start,ctl_stop,
             ctl_wild,ctl_limit,ctl_trip,ctl_recover} = config_word;
            #1;
            if ({ctl_ready,reply_valid,alert_valid} !== expected_handshake)
                $fatal(1,"handshake mismatch cycle %0d",cycle);
            if ({reply_status,reply_op,reply_rule,reply_revision,reply_data} !== expected_reply)
                $fatal(1,"reply mismatch cycle %0d expected %h received %h",cycle,expected_reply,
                       {reply_status,reply_op,reply_rule,reply_revision,reply_data});
            if ({alert_rule,alert_revision,alert_data} !== expected_alert)
                $fatal(1,"alert mismatch cycle %0d expected %h received %h",cycle,expected_alert,
                       {alert_rule,alert_revision,alert_data});
            if (alert_occupancy !== expected_occupancy)
                $fatal(1,"alert occupancy mismatch cycle %0d",cycle);
            if ({seen_reports,nonmatches,unclassified,lost_alerts} !== expected_stats)
                $fatal(1,"counter mismatch cycle %0d",cycle);
            if (sample_valid && !rst)
                reports++;
            if (reply_valid && reply_ready)
                replies++;
            if (alert_valid && alert_ready) begin
                alerts++;
                if (capture != 0)
                    $fdisplay(capture,"%0d %0d %064h",alert_rule,alert_revision,alert_data);
            end
            if (alert_valid && !alert_ready)
                stalls++;
            if (dut.lost)
                drops++;
            cycle++;
            @(posedge clk);
        end
        $fclose(vectors);
        if (capture != 0)
            $fclose(capture);
        if (reports < 10 || replies < 8 || alerts < 3 || stalls < 3 || drops < 2)
            $fatal(1,"incomplete exercised behavior");
        $display("deadline rules=%0d depth=%0d: %0d cycles, %0d reports, %0d replies, %0d visible alerts, %0d stalled, %0d dropped",
                 RULES,DEPTH,cycle,reports,replies,alerts,stalls,drops);
        $finish;
    end
endmodule

`default_nettype wire
