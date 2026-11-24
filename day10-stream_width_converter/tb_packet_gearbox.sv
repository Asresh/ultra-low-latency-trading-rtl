`timescale 1ns/1ps
`default_nettype none

module tb_packet_gearbox #(
    parameter integer IN_BYTES = 4,
    parameter integer OUT_BYTES = 8,
    parameter integer MAX_FRAME = 128
);
    logic clk = 0;
    always #5 clk = ~clk;
    logic rst = 1;
    logic s_valid = 0, s_last = 0, m_ready = 0, report_ready = 0;
    logic [IN_BYTES*8-1:0] s_data = 0;
    logic [IN_BYTES-1:0] s_keep = 0;
    logic [15:0] s_tag = 0;
    wire s_ready, m_valid, m_last, report_valid;
    wire [OUT_BYTES*8-1:0] m_data;
    wire [OUT_BYTES-1:0] m_keep;
    wire [15:0] m_tag, report_tag, report_bytes;
    wire [2:0] report_status;
    wire [31:0] accepted_packets, rejected_packets, emitted_packets, emitted_bytes;

    packet_gearbox #(.IN_BYTES(IN_BYTES), .OUT_BYTES(OUT_BYTES), .MAX_FRAME(MAX_FRAME)) dut (.*);

    integer trace, tape, fields, edges = 0;
    integer input_stalls = 0, output_stalls = 0, report_stalls = 0;
    integer simultaneous = 0;
    string trace_path, tape_path;
    logic want_ready, want_valid, want_last, want_report;
    logic [OUT_BYTES*8-1:0] want_data;
    logic [OUT_BYTES-1:0] want_keep;
    logic [15:0] want_tag, want_report_tag, want_bytes;
    logic [2:0] want_status;
    logic [31:0] want_accepted, want_rejected, want_emitted, want_output_bytes;

    // Compare every meaningful pre-edge signal after the model's inputs have settled.
    task automatic compare_sample;
        if ({s_ready,m_valid,report_valid} !== {want_ready,want_valid,want_report})
            $fatal(1,"Cycle %0d handshake s/m/report %b%b%b expected %b%b%b", edges,
                   s_ready,m_valid,report_valid,want_ready,want_valid,want_report);
        if (m_valid && {m_data,m_keep,m_last,m_tag} !== {want_data,want_keep,want_last,want_tag})
            $fatal(1,"Cycle %0d output mismatch data=%h/%h keep=%h/%h tag=%h/%h", edges,
                   m_data,want_data,m_keep,want_keep,m_tag,want_tag);
        if (report_valid && {report_tag,report_bytes,report_status} !==
                           {want_report_tag,want_bytes,want_status})
            $fatal(1,"Cycle %0d packet report mismatch",edges);
        if ({accepted_packets,rejected_packets,emitted_packets,emitted_bytes} !==
            {want_accepted,want_rejected,want_emitted,want_output_bytes})
            $fatal(1,"Cycle %0d counter mismatch",edges);
    endtask

    // Log actual transfers in a simple text format for the independent packet audit.
    task automatic log_transfers;
        if (rst)
            $fdisplay(trace,"reset %0d",edges);
        else begin
            if (s_valid && s_ready)
                $fdisplay(trace,"input %0d %h %h %b %h",edges,s_data,s_keep,s_last,s_tag);
            if (m_valid && m_ready)
                $fdisplay(trace,"output %0d %h %h %b %h",edges,m_data,m_keep,m_last,m_tag);
            if (report_valid && report_ready)
                $fdisplay(trace,"report %0d %h %h %h",edges,report_tag,report_bytes,report_status);
            input_stalls += int'(s_valid && !s_ready);
            output_stalls += int'(m_valid && !m_ready);
            report_stalls += int'(report_valid && !report_ready);
            simultaneous += int'(s_valid && s_ready && m_valid && m_ready);
        end
    endtask

    initial begin
        if (!$value$plusargs("tape=%s",tape_path) || !$value$plusargs("trace=%s",trace_path))
            $fatal(1,"Specify +tape= and +trace=");
        tape = $fopen(tape_path,"r");
        trace = $fopen(trace_path,"w");
        if (!tape || !trace) $fatal(1,"Could not open tape or trace");
        repeat (2) @(posedge clk);
        while (!$feof(tape)) begin
            @(negedge clk);
            fields = $fscanf(tape,
                "%h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h",
                rst,s_valid,s_data,s_keep,s_last,s_tag,m_ready,report_ready,
                want_ready,want_valid,want_data,want_keep,want_last,want_tag,
                want_report,want_report_tag,want_bytes,want_status,
                want_accepted,want_rejected,want_emitted,want_output_bytes);
            if (fields != 22 && !$feof(tape))
                $fatal(1,"Truncated fixture at cycle %0d, field count %0d",edges,fields);
            if (fields == 22) begin
                @(posedge clk);
                compare_sample();
                log_transfers();
                edges++;
            end
        end
        if (edges == 0 || (!$test$plusargs("small") &&
            (edges < 100 || input_stalls == 0 || output_stalls == 0 ||
            report_stalls == 0 || simultaneous == 0)))
            $fatal(1,"Fixture did not hit the necessary flow-control scenarios");
        $fclose(tape);
        $fclose(trace);
        $display("PASS gearbox %0d->%0d cap=%0d: %0d cycles, %0d input/%0d output/%0d report stalls, %0d duplex edges",
                 IN_BYTES,OUT_BYTES,MAX_FRAME,edges,input_stalls,output_stalls,report_stalls,simultaneous);
        $finish;
    end
    initial begin repeat (160000) @(posedge clk); $fatal(1,"Gearbox watchdog"); end

    // Optional trace of the testbench ports for the checked README example.
    string waveform_path;
    initial begin
        if ($value$plusargs("waveform=%s", waveform_path)) begin
            $dumpfile(waveform_path);
            $dumpvars(1, tb_packet_gearbox);
        end
    end
endmodule

`default_nettype wire
