`timescale 1ns/1ps
`default_nettype none

module tb_snapshot_receiver_demo;
    localparam integer SLOTS = 3, KEY_W = 2, EPOCH_W = 3, WAIT_W = 4;
    localparam integer MAX_ROWS = 8, DELTAS = 8, COUNT_W = 4, OCC_W = 4;
    localparam logic [79:0] OLD = "OLDBOOK001", PEER = "PEERBOOK01";
    logic clk = 0, rst = 1;
    always #5 clk = !clk;

    logic begin_valid = 0, begin_force;
    wire begin_ready;
    logic [31:0] begin_id, begin_crc, row_id, prepared_id;
    logic [KEY_W-1:0] begin_slot;
    logic [EPOCH_W-1:0] begin_expected_epoch;
    logic [79:0] begin_session;
    logic [63:0] begin_sequence;
    logic [COUNT_W-1:0] begin_rows;
    logic [WAIT_W-1:0] begin_spacing;
    logic row_valid = 0, row_last, prepared_valid = 0, prepared_ok = 0;
    logic [127:0] row_word;
    logic stage_ready = 0, allow_commit = 0;
    wire row_ready, restore_valid, prepared_ready;
    wire [1:0] restore_kind;
    wire restore_ready = restore_kind == 2 ? allow_commit : stage_ready;
    wire [31:0] restore_id, restore_crc;
    wire [KEY_W-1:0] restore_slot;
    wire [EPOCH_W-1:0] restore_epoch;
    wire [63:0] restore_sequence;
    wire [COUNT_W-1:0] restore_count, restore_index;
    wire [127:0] restore_word;

    wire cfg_valid, cfg_ready, cfg_enable, cfg_force, ack_valid, ack_ready, ack_active;
    wire [KEY_W-1:0] cfg_slot, ack_slot;
    wire [EPOCH_W-1:0] cfg_expected_epoch, ack_epoch;
    wire [79:0] cfg_session;
    wire [63:0] cfg_sequence;
    wire [WAIT_W-1:0] cfg_request_spacing;
    wire [1:0] ack_status;

    logic manual_mode = 1, manual_valid = 0, config_gate = 0;
    logic [KEY_W-1:0] init_slot;
    logic [79:0] init_session;
    logic [63:0] init_sequence;
    wire native_cfg_ready, native_ack_valid;
    assign cfg_ready = !manual_mode && config_gate && native_cfg_ready;
    assign ack_valid = !manual_mode && native_ack_valid;

    logic [1:0] feed_valid = 0, feed_first = 0, feed_last = 0;
    wire [1:0] feed_ready;
    logic [127:0] feed_data = 0;
    logic [15:0] feed_keep = 0;
    logic [2*KEY_W-1:0] feed_slots = 0;
    wire s_valid, s_ready;
    wire [KEY_W-1:0] s_slot;
    wire [EPOCH_W-1:0] s_epoch;
    wire [587:0] s_data;
    wire [63:0] input_sequence = s_data[583:520];

    multisession_market_data #(
        .SLOTS(SLOTS), .EPOCH_W(EPOCH_W), .WAIT_W(WAIT_W), .PACKET_BYTES(128),
        .BLOCKS(8), .WINDOW(8), .GAP_TICKS(32)
    ) receiver (
        .clk, .rst,
        .cfg_valid(manual_mode ? manual_valid : cfg_valid && config_gate),
        .cfg_ready(native_cfg_ready), .cfg_slot(manual_mode ? init_slot : cfg_slot),
        .cfg_expected_epoch(manual_mode ? EPOCH_W'(0) : cfg_expected_epoch),
        .cfg_enable(manual_mode || cfg_enable), .cfg_force(!manual_mode && cfg_force),
        .cfg_session(manual_mode ? init_session : cfg_session),
        .cfg_sequence(manual_mode ? init_sequence : cfg_sequence),
        .cfg_request_spacing(manual_mode ? WAIT_W'(0) : cfg_request_spacing),
        .ack_valid(native_ack_valid), .ack_ready(manual_mode || ack_ready),
        .ack_status, .ack_slot, .ack_epoch, .ack_active, .ack_cut_lanes(),
        .feed_valid, .feed_ready, .feed_data, .feed_keep, .feed_first, .feed_last, .feed_slots,
        .event_valid(s_valid), .event_ready(s_ready), .event_slot(s_slot), .event_epoch(s_epoch), .event_data(s_data),
        .request_valid(), .request_ready(1'b1), .request_slot(), .request_epoch(), .request_data(),
        .monitor_slot(KEY_W'(0)), .monitor_snapshot(1'b0), .monitor_index(4'd0),
        .monitor_valid(), .monitor_active(), .monitor_fenced(), .monitor_epoch(),
        .monitor_session(), .monitor_expected(), .monitor_high_water(), .monitor_gap(),
        .monitor_end(), .monitor_occupancy(), .monitor_statistic(), .monitor_peak(),
        .monitor_cooldown(), .monitor_packets(), .monitor_events(), .monitor_requests(), .discarded_packets()
    );

    logic m_ready = 1;
    wire m_valid;
    wire [KEY_W-1:0] m_slot;
    wire [EPOCH_W-1:0] m_epoch;
    wire [587:0] m_data;
    wire [63:0] output_sequence = m_data[583:520];
    wire done_valid;
    wire [31:0] done_id, done_actual_crc;
    wire [KEY_W-1:0] done_slot;
    wire [3:0] done_status, phase;
    wire [EPOCH_W-1:0] done_native_epoch, monitor_epoch;
    wire done_native_installed, done_book_committed, monitor_quarantined, scope_gap;
    wire [OCC_W-1:0] done_buffered, delta_used;
    wire [63:0] monitor_sequence;
    wire [31:0] committed_books, failed_cutovers;

    mold_snapshot_cutover #(
        .SLOTS(SLOTS), .EPOCH_W(EPOCH_W), .WAIT_W(WAIT_W), .MAX_ROWS(MAX_ROWS), .DELTAS(DELTAS)
    ) coordinator (
        .clk, .rst,
        .begin_valid, .begin_ready, .begin_id, .begin_slot, .begin_expected_epoch, .begin_session,
        .begin_sequence, .begin_rows, .begin_crc, .begin_force, .begin_spacing,
        .row_valid, .row_ready, .row_id, .row_word, .row_last,
        .restore_valid, .restore_ready, .restore_kind, .restore_id, .restore_slot, .restore_epoch,
        .restore_sequence, .restore_count, .restore_index, .restore_crc, .restore_word,
        .prepared_valid, .prepared_ready, .prepared_id, .prepared_ok,
        .cfg_valid, .cfg_ready, .cfg_slot, .cfg_expected_epoch, .cfg_enable, .cfg_force,
        .cfg_session, .cfg_sequence, .cfg_request_spacing,
        .ack_valid, .ack_ready, .ack_status, .ack_slot, .ack_epoch, .ack_active,
        .s_valid, .s_ready, .s_slot, .s_epoch, .s_data, .m_valid, .m_ready, .m_slot, .m_epoch, .m_data,
        .done_valid, .done_ready(1'b1), .done_id, .done_slot, .done_status, .done_native_epoch,
        .done_native_installed, .done_book_committed, .done_buffered, .done_actual_crc,
        .monitor_slot(KEY_W'(0)), .monitor_valid(), .monitor_known(), .monitor_quarantined,
        .monitor_epoch, .monitor_sequence, .monitor_stale(), .monitor_gap(), .monitor_delivered(),
        .monitor_buffered(), .invalid_slots(), .phase, .scope_gap, .delta_used, .candidate_sequence(),
        .foreign_rows(), .foreign_prepares(), .committed_books, .failed_cutovers
    );

    logic [63:0] recorded_data [0:3][0:63];
    logic [7:0] recorded_keep [0:3][0:63];
    logic [1:0] recorded_framing [0:3][0:63];
    integer recorded_count [0:3];
    integer commands, wire_file, restore_file, event_file, response_file;
    integer cycle = 0, row_transfers = 0, target_outputs = 0, peer_outputs = 0, old_outputs = 0;
    integer commit_cycle = -1, completions = 0, old_holds = 0;
    logic held_old = 0;
    always @(posedge clk) begin
        cycle++;
        if (!rst) begin
            if (restore_valid && restore_ready) begin
                $fdisplay(restore_file,"%h %h %h %h %h %h %h %h %h %h",
                          cycle,restore_kind,restore_id,restore_slot,restore_epoch,restore_sequence,
                          restore_count,restore_index,restore_crc,restore_word);
                row_transfers += restore_kind == 1;
                if (restore_kind == 2)
                    commit_cycle = cycle;
            end
            if (done_valid) begin
                completions++;
                $fdisplay(response_file,"%0d %0d %0d %0d %0d %0d %0d %0d",done_id,done_slot,
                          done_status,done_native_epoch,done_native_installed,
                          done_book_committed,done_buffered,done_actual_crc);
            end
            if (held_old && (!m_valid || m_slot != 0 || m_epoch != 1 || output_sequence != 90))
                $fatal(1,"target's already exported old epoch changed under a stall");
            held_old = m_valid && !m_ready && m_slot == 0 && m_epoch == 1;
            old_holds += held_old;
            if (m_valid && m_ready) begin
                $fdisplay(event_file,"%0d %h",cycle,{m_slot,m_epoch,m_data});
                if (m_slot == 1)
                    peer_outputs++;
                else if (m_epoch == 1)
                    old_outputs++;
                else begin
                    if (commit_cycle == -1 || cycle <= commit_cycle)
                        $fatal(1,"new slot exported a delta before the book commit");
                    target_outputs++;
                end
            end
        end
    end

    task automatic initial_config(input integer slot, input logic [79:0] session,
                                  input logic [63:0] seed);
        @(negedge clk);
        init_slot = KEY_W'(slot);
        init_session = session;
        init_sequence = seed;
        manual_valid = 1;
        do @(posedge clk); while (!native_cfg_ready);
        @(negedge clk);
        manual_valid = 0;
        while (!native_ack_valid)
            @(negedge clk);
        if (ack_status != 0 || ack_slot != KEY_W'(slot) || ack_epoch != 1 || !ack_active)
            $fatal(1,"cannot install old live receiver epoch");
        @(negedge clk);
    endtask

    task automatic send_group(input integer group_index, lane, slot);
        for (integer offset=0; offset<recorded_count[group_index]; offset++) begin
            @(negedge clk);
            feed_valid[lane] = 1;
            feed_slots[lane*KEY_W+:KEY_W] = KEY_W'(slot);
            feed_data[lane*64+:64] = recorded_data[group_index][offset];
            feed_keep[lane*8+:8] = recorded_keep[group_index][offset];
            feed_first[lane] = recorded_framing[group_index][offset][1];
            feed_last[lane] = recorded_framing[group_index][offset][0];
            do @(posedge clk); while (!feed_ready[lane]);
            @(negedge clk);
            feed_valid[lane] = 0;
        end
    endtask

    initial begin
        string directory, waves;
        integer fields;
        logic [63:0] data_lanes;
        logic [7:0] kept_lanes;
        logic [1:0] framing;
        if (!$value$plusargs("out=%s",directory))
            $fatal(1,"configure +out=DIR");
        if ($value$plusargs("waves=%s",waves)) begin
            $dumpfile(waves);
            $dumpvars(1,tb_snapshot_receiver_demo);
            $dumpvars(1,tb_snapshot_receiver_demo.coordinator);
        end
        commands = $fopen({directory,"/snapshot-commands.txt"},"r");
        wire_file = $fopen({directory,"/snapshot-wire.txt"},"r");
        restore_file = $fopen({directory,"/snapshot-restore.txt"},"w");
        event_file = $fopen({directory,"/snapshot-live.txt"},"w");
        response_file = $fopen({directory,"/snapshot-response.txt"},"w");
        if (!commands || !wire_file || !restore_file || !event_file || !response_file)
            $fatal(1,"cannot open integration fixtures or outputs");
        for (integer g=0; g<4; g++) begin
            if ($fscanf(wire_file,"%d",recorded_count[g]) != 1 ||
                recorded_count[g] < 1 || recorded_count[g] > 64)
                $fatal(1,"invalid wire group");
            for (integer b=0; b<recorded_count[g]; b++) begin
                fields = $fscanf(wire_file,"%h %h %h",data_lanes,kept_lanes,framing);
                if (fields != 3)
                    $fatal(1,"missing native datagram beat");
                recorded_data[g][b] = data_lanes;
                recorded_keep[g][b] = kept_lanes;
                recorded_framing[g][b] = framing;
            end
        end
        $fclose(wire_file);
        repeat (3) @(negedge clk);
        rst = 0;
        initial_config(0,OLD,90);
        initial_config(1,PEER,11);
        manual_mode = 0;

        fields = $fscanf(commands,"%h %h %h %h %h %h %h %h %h",begin_id,begin_slot,
                         begin_expected_epoch,begin_session,begin_sequence,begin_rows,
                         begin_crc,begin_force,begin_spacing);
        if (fields != 9 || begin_rows != 4)
            $fatal(1,"invalid production snapshot fixture");
        begin_valid = 1;
        do @(posedge clk); while (!begin_ready);
        @(negedge clk);
        begin_valid = 0;
        repeat (begin_rows) begin
            if ($fscanf(commands,"%h %h",row_word,row_last) != 2)
                $fatal(1,"truncated compiled snapshot");
            row_id = begin_id;
            row_valid = 1;
            do @(posedge clk); while (!row_ready);
            @(negedge clk);
            row_valid = 0;
        end
        $fclose(commands);
        if (!restore_valid || restore_kind != 0)
            $fatal(1,"validated snapshot never reached the book sink");
        m_ready = 0;
        send_group(0,0,0);
        while (!m_valid)
            @(negedge clk);
        stage_ready = 1;
        while (!prepared_ready)
            @(negedge clk);
        prepared_id = begin_id;
        prepared_ok = 1;
        prepared_valid = 1;
        @(negedge clk);
        prepared_valid = 0;
        repeat (8) begin
            if (cfg_valid || phase != 5)
                $fatal(1,"receiver changed before the old held output drained");
            @(negedge clk);
        end
        m_ready = 1;
        while (!cfg_valid)
            @(negedge clk);

        // With the native command deliberately held, unrelated live traffic
        // continues through the coordinator on the existing receiver port.
        send_group(1,1,1);
        while (peer_outputs != 2)
            @(negedge clk);
        config_gate = 1;
        while (!restore_valid || restore_kind != 2)
            @(negedge clk);
        send_group(2,0,0);
        while (delta_used != 4)
            @(negedge clk);
        if (commit_cycle != -1 || target_outputs != 0 || scope_gap)
            $fatal(1,"next-epoch output crossed the snapshot commit barrier");
        repeat (3) @(negedge clk);
        allow_commit = 1;
        while (target_outputs != 4)
            @(negedge clk);
        send_group(3,0,0);
        while (target_outputs != 5)
            @(negedge clk);
        repeat (4) @(negedge clk);
        if (row_transfers != 4 || completions != 1 || old_outputs != 1 ||
            peer_outputs != 2 || old_holds < 8 || committed_books != 1 || failed_cutovers != 0 ||
            monitor_quarantined || monitor_epoch != 2 || monitor_sequence != 3)
            $fatal(1,"snapshot integration metrics disagree");
        $fclose(restore_file);
        $fclose(event_file);
        $fclose(response_file);
        $display("PASS native snapshot cutover: %0d restore rows, %0d new deltas, %0d peer events, %0d old output holds",
                 row_transfers,target_outputs,peer_outputs,old_holds);
        $finish;
    end

    initial begin
        #30000;
        $fatal(1,"native snapshot integration timed out");
    end
endmodule

`default_nettype wire
