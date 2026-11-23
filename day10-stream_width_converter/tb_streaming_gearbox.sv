`timescale 1ns/1ps
`default_nettype none

module tb_streaming_gearbox #(
    parameter integer IN_BYTES = 4,
    parameter integer OUT_BYTES = 8,
    parameter integer CAPACITY = IN_BYTES + OUT_BYTES
);
    localparam integer COUNT_W = $clog2(CAPACITY+1);
    logic clk = 0;
    always #5 clk = ~clk;
    logic rst = 1, s_valid = 0, s_last = 0, m_ready = 0;
    logic [IN_BYTES*8-1:0] s_data = '0;
    logic [IN_BYTES-1:0] s_keep = '0;
    logic [15:0] s_tag = '0;
    wire s_ready, m_valid, m_last;
    wire [OUT_BYTES*8-1:0] m_data;
    wire [OUT_BYTES-1:0] m_keep;
    wire [15:0] m_tag;
    wire [COUNT_W-1:0] occupancy;

    streaming_gearbox #(.IN_BYTES(IN_BYTES),.OUT_BYTES(OUT_BYTES),.CAPACITY(CAPACITY)) dut (.*);

    logic want_ready, want_valid, want_last;
    logic [OUT_BYTES*8-1:0] want_data;
    logic [OUT_BYTES-1:0] want_keep;
    logic [15:0] want_tag;
    logic [COUNT_W-1:0] want_occupancy;
    logic input_seen_final = 0;
    integer fields, tape, cycle = 0, packets = 0, true_cut_through = 0;
    integer saturated = 0, held = 0, concurrent_bytes = 0;
    string source;

    // Check one edge and retain evidence of output that precedes the input last.
    task automatic check_edge;
        if ({s_ready,m_valid,occupancy} !== {want_ready,want_valid,want_occupancy})
            $fatal(1,"Streaming state mismatch cycle %0d, used=%0d expected %0d",
                   cycle,occupancy,want_occupancy);
        if (m_valid && {m_data,m_keep,m_last,m_tag} !== {want_data,want_keep,want_last,want_tag})
            $fatal(1,"Stream output mismatch cycle %0d tag=%h expected %h",cycle,m_tag,want_tag);
        if (s_valid && !s_ready) saturated++;
        if (m_valid && !m_ready) held++;
        if (s_valid && s_ready && m_valid && m_ready) concurrent_bytes++;
        if (m_valid && m_ready && !m_last && s_tag == m_tag &&
            s_valid && s_ready && !s_last && !input_seen_final)
            true_cut_through++;
        if (s_valid && s_ready && s_last) input_seen_final = 1;
        if (s_valid && s_ready && !s_last && !m_last) input_seen_final = 0;
        if (m_valid && m_ready && m_last) packets++;
    endtask

    initial begin
        if (!$value$plusargs("tape=%s",source)) $fatal(1,"Pass +tape=<path>");
        tape = $fopen(source,"r");
        if (!tape) $fatal(1,"Unable to open streaming fixture");
        repeat (2) @(posedge clk);
        while (!$feof(tape)) begin
            @(negedge clk);
            fields = $fscanf(tape,"%h %h %h %h %h %h %h %h %h %h %h %h %h %h",
                rst,s_valid,s_data,s_keep,s_last,s_tag,m_ready,want_ready,want_valid,
                want_data,want_keep,want_last,want_tag,want_occupancy);
            if (fields != 14 && !$feof(tape)) $fatal(1,"Bad streaming fixture record %0d",cycle);
            if (fields == 14) begin
                @(posedge clk);
                check_edge();
                cycle++;
            end
        end
        $fclose(tape);
        if (packets != 160 || !saturated || !held || !concurrent_bytes || !true_cut_through)
            $fatal(1,"Missing cut-through workload coverage");
        $display("PASS cut-through %0d->%0d depth=%0d %0d packets %0d cycles %0d early output edges",
                 IN_BYTES,OUT_BYTES,CAPACITY,packets,cycle,true_cut_through);
        $finish;
    end
    initial begin #2500000; $fatal(1,"Cut-through watchdog"); end
endmodule

`default_nettype wire
