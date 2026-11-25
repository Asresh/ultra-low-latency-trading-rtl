`timescale 1ns/1ps
`default_nettype none

module tb_mold_snapshot_cutover #(
    parameter integer SLOTS = 3,
    parameter integer EPOCH_W = 3,
    parameter integer WAIT_W = 4,
    parameter integer MAX_ROWS = 5,
    parameter integer DELTAS = 3
);
    localparam integer KEY_W = $clog2(SLOTS+1);
    localparam integer COUNT_W = $clog2(MAX_ROWS+2);
    localparam integer OCC_W = $clog2(DELTAS+1);
    localparam integer BEGIN_W = 209+KEY_W+EPOCH_W+COUNT_W+WAIT_W;
    localparam integer RESTORE_W = 258+KEY_W+EPOCH_W+2*COUNT_W;
    localparam integer EVENT_W = 588+KEY_W+EPOCH_W;
    localparam integer CFG_W = 146+KEY_W+EPOCH_W+WAIT_W;
    localparam integer DONE_W = 70+KEY_W+EPOCH_W+OCC_W;
    localparam integer MON_W = 194+EPOCH_W;
    localparam integer LIVE_W = 229+OCC_W;

    logic clk = 0, rst = 1;
    logic begin_valid = 0, row_valid = 0, prepared_valid = 0, ack_valid = 0, s_valid = 0;
    logic restore_ready = 0, cfg_ready = 0, m_ready = 0, done_ready = 0;
    logic [31:0] begin_id, begin_crc, row_id, prepared_id;
    logic [KEY_W-1:0] begin_slot, ack_slot, s_slot, monitor_slot;
    logic [EPOCH_W-1:0] begin_expected_epoch, ack_epoch, s_epoch;
    logic [79:0] begin_session;
    logic [63:0] begin_sequence;
    logic [COUNT_W-1:0] begin_rows;
    logic begin_force, row_last, prepared_ok, ack_active;
    logic [WAIT_W-1:0] begin_spacing;
    logic [127:0] row_word;
    logic [1:0] ack_status;
    logic [587:0] s_data;

    wire begin_ready, row_ready, restore_valid, prepared_ready, cfg_valid, ack_ready, s_ready, m_valid;
    wire done_valid, monitor_valid, monitor_known, monitor_quarantined;
    wire [1:0] restore_kind;
    wire [31:0] restore_id, restore_crc, done_id, done_actual_crc;
    wire [KEY_W-1:0] restore_slot, cfg_slot, m_slot, done_slot;
    wire [EPOCH_W-1:0] restore_epoch, cfg_expected_epoch, m_epoch, done_native_epoch, monitor_epoch;
    wire [63:0] restore_sequence, cfg_sequence, monitor_sequence, candidate_sequence;
    wire [COUNT_W-1:0] restore_count, restore_index;
    wire [127:0] restore_word;
    wire cfg_enable, cfg_force, done_native_installed, done_book_committed, scope_gap;
    wire [79:0] cfg_session;
    wire [WAIT_W-1:0] cfg_request_spacing;
    wire [587:0] m_data;
    wire [3:0] phase, done_status;
    wire [OCC_W-1:0] delta_used, done_buffered;
    wire [31:0] monitor_stale, monitor_gap, monitor_delivered, monitor_buffered, invalid_slots;
    wire [31:0] foreign_rows, foreign_prepares, committed_books, failed_cutovers;
    wire [63:0] output_sequence = m_data[583:520];

    always #5 clk = !clk;
    mold_snapshot_cutover #(
        .SLOTS(SLOTS), .EPOCH_W(EPOCH_W), .WAIT_W(WAIT_W), .MAX_ROWS(MAX_ROWS), .DELTAS(DELTAS)
    ) dut (.*);

    wire [RESTORE_W-1:0] restore_fields = {
        restore_kind,restore_id,restore_slot,restore_epoch,restore_sequence,restore_count,
        restore_index,restore_crc,restore_word
    };
    wire [CFG_W-1:0] cfg_fields = {
        cfg_slot,cfg_expected_epoch,cfg_enable,cfg_force,cfg_session,cfg_sequence,cfg_request_spacing
    };
    wire [DONE_W-1:0] done_fields = {
        done_id,done_slot,done_status,done_native_epoch,done_native_installed,
        done_book_committed,done_buffered,done_actual_crc
    };
    wire [MON_W-1:0] monitor_fields = {
        monitor_known,monitor_quarantined,monitor_epoch,monitor_sequence,
        monitor_stale,monitor_gap,monitor_delivered,monitor_buffered
    };
    wire [LIVE_W-1:0] live_fields = {
        phase,scope_gap,delta_used,candidate_sequence,invalid_slots,foreign_rows,foreign_prepares,
        committed_books,failed_cutovers
    };

    logic [9:0] flags, expected_flags;
    logic [BEGIN_W-1:0] command_word;
    logic [160:0] input_row;
    logic [32:0] input_prepare;
    logic [2+KEY_W+EPOCH_W:0] input_ack;
    logic [EVENT_W-1:0] input_event, expected_event;
    logic [RESTORE_W-1:0] expected_restore;
    logic [CFG_W-1:0] expected_config;
    logic [DONE_W-1:0] expected_done;
    logic [MON_W-1:0] expected_monitor;
    logic [LIVE_W-1:0] expected_live;
    logic [8:0] status_seen = 0;
    logic [3:0] records_seen = 0;
    logic [9:0] phases_seen = 0;
    integer file, fields, index = 0, restored = 0, native = 0, events = 0, results = 0;
    integer restore_holds = 0, config_holds = 0, output_holds = 0, result_holds = 0;
    integer full = 0, late_cutover_gaps = 0;
    string fixture, wave;

    initial begin
        if (!$value$plusargs("vectors=%s",fixture))
            $fatal(1,"configure +vectors=PATH");
        file = $fopen(fixture,"r");
        if (!file)
            $fatal(1,"could not open %s",fixture);
        if ($value$plusargs("waves=%s",wave)) begin
            $dumpfile(wave);
            $dumpvars(1,tb_mold_snapshot_cutover);
            $dumpvars(1,tb_mold_snapshot_cutover.dut);
        end

        @(posedge clk);
        while (!$feof(file)) begin
            @(negedge clk);
            fields = $fscanf(file,"%h %h %h %h %h %h %h %h %h %h %h %h %h %h\n",
                flags,command_word,input_row,input_prepare,input_ack,input_event,monitor_slot,
                expected_flags,expected_restore,expected_config,expected_event,expected_done,
                expected_monitor,expected_live);
            if (fields != 14)
                $fatal(1,"fixture row %0d contained %0d fields",index,fields);
            {rst,begin_valid,row_valid,prepared_valid,ack_valid,s_valid,restore_ready,
             cfg_ready,m_ready,done_ready} = flags;
            {begin_id,begin_slot,begin_expected_epoch,begin_session,begin_sequence,begin_rows,
             begin_crc,begin_force,begin_spacing} = command_word;
            {row_id,row_word,row_last} = input_row;
            {prepared_id,prepared_ok} = input_prepare;
            {ack_status,ack_slot,ack_epoch,ack_active} = input_ack;
            {s_slot,s_epoch,s_data} = input_event;
            #1;
            if ({begin_ready,row_ready,prepared_ready,ack_ready,s_ready,restore_valid,cfg_valid,
                 m_valid,done_valid,monitor_valid} !== expected_flags)
                $fatal(1,"handshake row %0d phase=%0d expected %b observed %b",index,phase,expected_flags,
                       {begin_ready,row_ready,prepared_ready,ack_ready,s_ready,restore_valid,cfg_valid,
                        m_valid,done_valid,monitor_valid});
            if (restore_valid && restore_fields !== expected_restore)
                $fatal(1,"book transaction row %0d expected %h observed %h",
                       index,expected_restore,restore_fields);
            if (cfg_valid && cfg_fields !== expected_config)
                $fatal(1,"native command row %0d differs",index);
            if (m_valid && {m_slot,m_epoch,m_data} !== expected_event)
                $fatal(1,"live event row %0d differs",index);
            if (done_valid && done_fields !== expected_done)
                $fatal(1,"completion row %0d expected %h observed %h",index,expected_done,done_fields);
            if (monitor_valid && monitor_fields !== expected_monitor)
                $fatal(1,"monitor row %0d expected %h observed %h",index,expected_monitor,monitor_fields);
            if (live_fields !== expected_live)
                $fatal(1,"controller row %0d expected %h observed %h",index,expected_live,live_fields);
            if (!rst) begin
                phases_seen[phase] = 1;
                restore_holds += restore_valid && !restore_ready;
                config_holds += cfg_valid && !cfg_ready;
                output_holds += m_valid && !m_ready;
                result_holds += done_valid && !done_ready;
                full += s_valid && !s_ready && delta_used == OCC_W'(DELTAS);
                events += m_valid && m_ready;
                native += cfg_valid && cfg_ready;
                if (restore_valid && restore_ready) begin
                    records_seen[restore_kind] = 1;
                    restored += restore_kind == 1;
                end
                if (done_valid && done_ready) begin
                    status_seen[done_status] = 1;
                    results++;
                    late_cutover_gaps += done_status == 8 && done_book_committed;
                end
            end
            index++;
            @(posedge clk);
        end
        $fclose(file);
        if (status_seen !== 9'h1ff || records_seen !== 4'hf || phases_seen !== 10'h3ff ||
            restore_holds < 5 || config_holds < 5 || output_holds < 8 || result_holds < 5 ||
            full < 5 || !late_cutover_gaps)
            $fatal(1,"coverage statuses=%h records=%h phases=%h late=%0d",
                   status_seen,records_seen,phases_seen,late_cutover_gaps);
        $display("PASS snapshot cutover: %0d cycles, %0d results, %0d restored rows, %0d native commands, %0d live outputs, %0d full holds, %0d/%0d/%0d/%0d channel stalls",
                 index,results,restored,native,events,full,restore_holds,config_holds,output_holds,result_holds);
        $finish;
    end
endmodule

`default_nettype wire
