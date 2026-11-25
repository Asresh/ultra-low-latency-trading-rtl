`timescale 1ns/1ps
`default_nettype none
`include "reconcile_defs.svh"

module tb_drop_copy_investigator #(
    parameter integer NAMESPACES = 3,
    parameter integer SETS = 4,
    parameter integer WAIT_CYCLES = 24,
    parameter integer HISTORY_CYCLES = 96,
    localparam integer NS_W = $clog2(NAMESPACES+1),
    localparam integer COUNT_W = $clog2(2*SETS+1),
    localparam integer REPORT_W = NS_W+4+2+`DC_KEY_W+2*`DC_PAYLOAD_W+4+32+COUNT_W,
    localparam integer EVENT_W = NS_W+`DC_KEY_W+`DC_PAYLOAD_W,
    localparam integer EPOCH_REQ_W = NS_W+16,
    localparam integer MARK_W = 1+32+32+NAMESPACES*(16+COUNT_W+1)+32,
    localparam integer RECORD_W = 3*64+5+2*EVENT_W+EPOCH_REQ_W+REPORT_W+MARK_W
);
    logic clk = 0;
    logic rst, m_ready, audit_allow, serial_ready;
    logic [63:0] run_identity;
    logic s_primary_valid, s_copy_valid, s_epoch_valid, mark_valid, mark_terminal;
    logic [NS_W-1:0] s_primary_namespace,s_copy_namespace,s_epoch_namespace;
    logic [`DC_KEY_W-1:0] s_primary_key,s_copy_key;
    logic [`DC_PAYLOAD_W-1:0] s_primary_payload,s_copy_payload;
    logic [15:0] s_new_epoch;
    logic [31:0] mark_tag;
    wire s_primary_ready,s_copy_ready,s_epoch_ready,mark_ready;
    wire m_valid,audit_valid,audit_ready,sealed,serial_valid,serial_first,serial_last,format_ready;
    wire [NS_W-1:0] m_namespace;
    wire [3:0] m_status,m_diff;
    wire [1:0] m_source;
    wire [`DC_KEY_W-1:0] m_key;
    wire [`DC_PAYLOAD_W-1:0] m_expected,m_observed;
    wire [31:0] m_age,bad_namespace_count,serial_word;
    wire [COUNT_W-1:0] m_flushed;
    wire [63:0] m_record_index,audit_index;
    wire [RECORD_W-1:0] audit_record;
    wire [NAMESPACES*16-1:0] active_epochs;
    wire [NAMESPACES*COUNT_W-1:0] pending_counts;
    wire [NAMESPACES*320-1:0] namespace_metrics;
    wire [NAMESPACES-1:0] namespace_incidents;

    drop_copy_investigator #(
        .NAMESPACES(NAMESPACES), .SETS(SETS), .WAIT_CYCLES(WAIT_CYCLES),
        .HISTORY_CYCLES(HISTORY_CYCLES), .RESET_TICK(32'hffff_fff0)
    ) dut (.*);
    drop_copy_audit_frame #(.RECORD_W(RECORD_W)) serializer (
        .clk, .rst, .s_valid(audit_valid && audit_allow), .s_ready(format_ready),
        .s_record(audit_record), .m_valid(serial_valid), .m_ready(serial_ready),
        .m_word(serial_word), .m_first(serial_first), .m_last(serial_last)
    );
    assign audit_ready = audit_allow && format_ready;

    wire [REPORT_W+63:0] diagnostic = {
        m_record_index,m_namespace,m_status,m_source,m_key,m_expected,m_observed,m_diff,m_age,m_flushed
    };
    wire [33:0] formatted = {serial_first,serial_last,serial_word};
    logic [REPORT_W+63:0] held_diag;
    logic [RECORD_W-1:0] held_record;
    logic [33:0] held_word;
    bit diag_stalled = 0, audit_stalled = 0, serial_stalled = 0;
    integer observed, output_tape;

    task automatic sample(input integer cycle);
        begin
            if (!rst) begin
                if (diag_stalled && (!m_valid || diagnostic !== held_diag))
                    $fatal(1,"diagnostic changed under stall at cycle %0d",cycle);
                if (audit_stalled && (!audit_valid || audit_record !== held_record))
                    $fatal(1,"journal changed under stall at cycle %0d",cycle);
                if (serial_stalled && (!serial_valid || formatted !== held_word))
                    $fatal(1,"serialized audit changed under stall at cycle %0d",cycle);
            end
            diag_stalled = !rst && m_valid && !m_ready;
            audit_stalled = !rst && audit_valid && !audit_ready;
            serial_stalled = !rst && serial_valid && !serial_ready;
            held_diag = diagnostic;
            held_record = audit_record;
            held_word = formatted;

            $fwrite(observed,"%h %h %h %h ",s_primary_ready,s_copy_ready,s_epoch_ready,mark_ready);
            if (m_valid)
                $fwrite(observed,"1 %h %h %h %h %h %h %h %h %h %h ",m_record_index,
                    m_namespace,m_status,m_source,m_key,m_expected,m_observed,m_diff,m_age,m_flushed);
            else
                $fwrite(observed,"0 0 0 0 0 0 0 0 0 0 0 ");
            if (audit_valid)
                $fwrite(observed,"1 %h ",audit_record);
            else
                $fwrite(observed,"0 0 ");
            $fwrite(observed,"%h %h ",rst ? 64'd0 : audit_index,!rst && sealed);
            if (serial_valid)
                $fwrite(observed,"1 %h %h %h ",serial_first,serial_last,serial_word);
            else
                $fwrite(observed,"0 0 0 0 ");
            if (rst)
                $fwrite(observed,"0 0 0 0 0\n");
            else
                $fwrite(observed,"%h %h %h %h %h\n",active_epochs,pending_counts,namespace_metrics,
                    namespace_incidents,bad_namespace_count);

            if (serial_valid && serial_ready)
                $fdisplay(output_tape,"%08h",serial_word);
        end
    endtask

    string directory,wave_path;
    integer source,count,fields;
    initial begin
        if (!$value$plusargs("vectors=%s",directory))
            $fatal(1,"missing vectors=directory");
        if ($value$plusargs("waves=%s",wave_path)) begin
            $dumpfile(wave_path);
            $dumpvars(1,tb_drop_copy_investigator);
        end
        source = $fopen({directory,"/drive.hex"},"r");
        observed = $fopen({directory,"/observed.hex"},"w");
        output_tape = $fopen({directory,"/hardware_tape.hex"},"w");
        if (!source || !observed || !output_tape)
            $fatal(1,"could not open audit fixture files");
        if ($fscanf(source,"%d",count) != 1 || count < 1)
            $fatal(1,"empty audit fixture");
        $fdisplay(observed,"%0d",count);
        for (integer cycle=0; cycle<count; cycle++) begin
            fields = $fscanf(source,"%h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h",
                             rst,run_identity,m_ready,audit_allow,serial_ready,
                             s_primary_valid,s_primary_namespace,s_primary_key,s_primary_payload,
                             s_copy_valid,s_copy_namespace,s_copy_key,s_copy_payload,
                             s_epoch_valid,s_epoch_namespace,s_new_epoch,mark_valid,mark_tag,mark_terminal);
            if (fields != 19)
                $fatal(1,"bad stimulus row %0d",cycle);
            if (cycle == 0 && !rst)
                $fatal(1,"audit fixture requires initial reset");
            #2;
            sample(cycle);
            clk = 1;
            #2;
            clk = 0;
        end
        $fclose(source);
        $fclose(observed);
        $fclose(output_tape);
        $display("Sampled %0d audit cycles with %0d namespaces, %0d sets, %0d record bits",
                 count,NAMESPACES,SETS,RECORD_W);
        $finish;
    end
endmodule

`default_nettype wire
