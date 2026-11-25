`timescale 1ns/1ps
`default_nettype none

module tb_ouch_order_session #(
    parameter integer ORDER_SLOTS = 8,
    parameter integer REPLAY_HISTORY_DEPTH = 0,
    parameter bit RECOVERABLE = 0
);
    import ouch_wire_pkg::*;
    localparam integer NOTICE_BITS = $bits(notice_t);
    localparam integer SNAPSHOT_BITS = 3 + 8 + 80 + 64 + 4 + 32 + 8 + 8 +
                                      $bits(slot_view_t) + $bits(metrics_t);
    localparam integer RAW_BITS = 2048;
    localparam integer WIRE_BITS = (MAX_BODY + 2) * 8;
    localparam integer LOGIN_BITS = $bits(login_t);
    localparam integer CMD_BITS = $bits(command_t);

    logic clk = 0;
    always #5 clk = ~clk;

    logic rst, transport_up, time_tick, kill_req;
    wire kill_latched, close_transport;
    logic s_login_valid, s_logout_valid, s_cmd_valid, s_rx_valid;
    wire s_login_ready, s_logout_ready, s_cmd_ready, s_rx_ready;
    login_t s_login;
    command_t s_cmd;
    logic [63:0] s_rx_data;
    logic [7:0] s_rx_keep;

    wire m_tx_valid, m_tx_last, m_notice_valid;
    logic m_notice_ready;
    wire m_tx_ready;
    logic random_tx_ready;
    wire [63:0] m_tx_data;
    wire [7:0] m_tx_keep;
    notice_t m_notice;

    logic [7:0] query_slot;
    slot_view_t query_view;
    metrics_t metrics;
    session_state_t session_state;
    status_t last_fault;
    wire [79:0] session_id;
    wire [63:0] next_sequence;
    wire replaying, market_open;
    wire [31:0] highest_order_ref;
    wire [7:0] live_count, pending_count;

    logic cp_cmd_valid = 0, cp_abort_import = 0, cp_in_valid = 0;
    logic cp_in_last = 0, cp_out_ready = 0, cp_result_ready = 0;
    logic [1:0] cp_cmd_op;
    logic [63:0] cp_image_id;
    logic [31:0] cp_in_data;
    wire cp_cmd_ready, cp_in_ready, cp_out_valid, cp_out_last, cp_result_valid;
    wire [31:0] cp_out_data;
    wire [3:0] cp_result_status;
    wire [1:0] cp_result_op;
    wire [63:0] cp_result_id;
    wire [7:0] cp_bad_slot;
    wire checkpoint_busy, boot_open;
    wire replay_fence_probe;
    wire [63:0] socket_cursor_probe;

    if (RECOVERABLE) begin : g_checkpoint
        ouch_recoverable_session #(.ORDER_SLOTS(ORDER_SLOTS), .TX_HEARTBEAT_TICKS(4),
            .RX_TIMEOUT_TICKS(7), .REPLAY_HISTORY_DEPTH(REPLAY_HISTORY_DEPTH)) dut (.*);
        assign replay_fence_probe = dut.engine.replay_fenced;
        assign socket_cursor_probe = dut.engine.socket_cursor;
    end else begin : g_legacy
        ouch_order_session #(.ORDER_SLOTS(ORDER_SLOTS), .TX_HEARTBEAT_TICKS(4),
            .RX_TIMEOUT_TICKS(7), .REPLAY_HISTORY_DEPTH(REPLAY_HISTORY_DEPTH)) dut (.*);
        assign replay_fence_probe = dut.engine.replay_fenced;
        assign socket_cursor_probe = dut.engine.socket_cursor;
        assign {cp_cmd_ready,cp_in_ready,cp_out_valid,cp_out_last,cp_result_valid,cp_out_data,
                cp_result_status,cp_result_op,cp_result_id,cp_bad_slot,checkpoint_busy,boot_open} = '0;
    end

    wire [SNAPSHOT_BITS-1:0] snapshot = {
        session_state, last_fault, session_id, next_sequence, replaying, market_open,
        kill_latched, close_transport, highest_order_ref, live_count, pending_count,
        query_view, metrics
    };

    // Generate the next deterministic state for the test's randomized choices.
    function automatic logic [31:0] advance(input logic [31:0] value);
        logic [31:0] x;
        begin
            x = value ^ (value << 13);
            x = x ^ (x >> 17);
            advance = x ^ (x << 5);
        end
    endfunction

    logic [31:0] stalls = 32'h0047_3e09;
    logic [31:0] fragments = 32'h7eaf_001a;
    integer stall_seed;
    integer tx_allowance = -1, tx_beat_count = 0;
    assign m_tx_ready = random_tx_ready && (tx_allowance < 0 || tx_beat_count < tx_allowance);
    bit hold_notices = 0;
    always @(negedge clk) begin
        stalls <= advance(stalls);
        random_tx_ready <= stalls[2:0] < 5;
        m_notice_ready <= !hold_notices && stalls[7:4] < 10;
    end

    integer tx_file, notice_file, stim_file, capture_file, aborted_file;
    integer tx_seen = 0, notices_seen = 0, accepted_beats = 0;
    integer receive_stalls = 0, tx_stalls = 0, notice_stalls = 0;
    integer checked_aborts = 0;
    integer observed_packet_length = 0;
    integer expected_packet_length;
    integer columns;
    logic [WIRE_BITS-1:0] observed_packet, expected_packet;
    logic [NOTICE_BITS-1:0] expected_notice;
    logic [72:0] held_tx;
    logic [NOTICE_BITS-1:0] held_notice;
    bit prior_tx_stall = 0, prior_notice_stall = 0;
    bit prior_rx_stall = 0;
    logic [71:0] held_rx;

    always @(posedge clk) begin
        if (rst || !transport_up) begin
            if (m_tx_valid || s_rx_ready)
                $fatal(1, "transport flush did not suppress byte handshakes");
            if (observed_packet_length != 0) begin
                columns = $fscanf(aborted_file, "%d %h", expected_packet_length, expected_packet);
                if (columns != 2 || observed_packet_length != expected_packet_length ||
                    observed_packet !== expected_packet)
                    $fatal(1, "aborted TX prefix disagrees with trace");
                checked_aborts = checked_aborts + 1;
                observed_packet_length = 0;
            end
            observed_packet = '0;
            prior_tx_stall = 0;
            prior_rx_stall = 0;
        end else begin
            if (prior_tx_stall &&
                (!m_tx_valid || {m_tx_last, m_tx_keep, m_tx_data} !== held_tx))
                $fatal(1, "held TX beat changed under backpressure");
            if (prior_rx_stall && (!s_rx_valid || {s_rx_keep, s_rx_data} !== held_rx))
                $fatal(1, "source mutated a stalled RX beat");
            if (m_tx_valid && m_tx_ready) begin
                tx_beat_count = tx_beat_count + 1;
                if (!m_tx_keep || ((m_tx_keep & (m_tx_keep + 1'b1)) != 0) ||
                    (!m_tx_last && m_tx_keep != 8'hff))
                    $fatal(1, "TX emitted malformed keep %b", m_tx_keep);
                for (integer i = 0; i < 8; i = i + 1) begin
                    if (m_tx_keep[i]) begin
                        if (observed_packet_length >= MAX_BODY + 2)
                            $fatal(1, "TX packet exceeded maximum bytes");
                        observed_packet[observed_packet_length*8 +: 8] = m_tx_data[i*8 +: 8];
                        observed_packet_length = observed_packet_length + 1;
                    end
                end
                if (m_tx_last) begin
                    columns = $fscanf(tx_file, "%d %h", expected_packet_length, expected_packet);
                    if (columns != 2) $fatal(1, "unexpected TX frame %0d", tx_seen);
                    if (observed_packet_length != expected_packet_length ||
                        observed_packet !== expected_packet) begin
                        $display("expected %0d byte frame %h", expected_packet_length, expected_packet);
                        $display("observed %0d byte frame %h", observed_packet_length, observed_packet);
                        $fatal(1, "wire bytes disagree at TX frame %0d", tx_seen);
                    end
                    $fdisplay(capture_file, "%0d %h", observed_packet_length, observed_packet);
                    observed_packet_length = 0;
                    observed_packet = '0;
                    tx_seen = tx_seen + 1;
                end
            end
            if (s_rx_valid && s_rx_ready)
                accepted_beats = accepted_beats + 1;
            if (m_tx_valid && !m_tx_ready) tx_stalls = tx_stalls + 1;
            if (s_rx_valid && !s_rx_ready) receive_stalls = receive_stalls + 1;
            prior_tx_stall = m_tx_valid && !m_tx_ready;
            prior_rx_stall = s_rx_valid && !s_rx_ready;
            held_tx = {m_tx_last, m_tx_keep, m_tx_data};
            held_rx = {s_rx_keep, s_rx_data};
        end

        if (rst) begin
            if (m_notice_valid || s_cmd_ready || s_login_ready || s_logout_ready)
                $fatal(1, "application channel handshake during reset");
            prior_notice_stall = 0;
        end else begin
            if (prior_notice_stall && (!m_notice_valid || m_notice !== held_notice))
                $fatal(1, "notice output changed before acknowledgment");
            if (m_notice_valid && m_notice_ready) begin
                columns = $fscanf(notice_file, "%h", expected_notice);
                if (columns != 1) $fatal(1, "unexpected notice %0d", notices_seen);
                if (m_notice !== expected_notice) begin
                    $display("observed kind %0d status %0d seq %0d ref %0d slot %0d tag %0d",
                             m_notice.kind, m_notice.status, m_notice.sequence_id,
                             m_notice.order_ref, m_notice.slot, m_notice.tag);
                    $display("expected %h; observed %h", expected_notice, m_notice);
                    $fatal(1, "mismatched notice %0d", notices_seen);
                end
                notices_seen = notices_seen + 1;
            end
            if (m_notice_valid && !m_notice_ready) notice_stalls = notice_stalls + 1;
            prior_notice_stall = m_notice_valid && !m_notice_ready;
            held_notice = m_notice;
        end
    end

    // Apply a synchronous reset and return the interface drivers to idle.
    task automatic reset_core;
        begin
            @(negedge clk);
            rst = 1;
            repeat (3) @(negedge clk);
            rst = 0;
        end
    endtask

    // Drive the link availability signal at the next safe edge.
    task automatic set_transport(input integer up);
        begin
            @(negedge clk);
            transport_up = up != 0;
            repeat (3) @(negedge clk);
        end
    endtask

    // Present one packed login request and wait for its input handshake.
    task automatic send_login(input logic [LOGIN_BITS-1:0] request);
        begin
            @(negedge clk);
            s_login = request;
            s_login_valid = 1;
            do @(posedge clk); while (!s_login_ready);
            @(negedge clk);
            s_login_valid = 0;
        end
    endtask

    // Present one application command and wait until the session accepts it.
    task automatic send_command(input logic [CMD_BITS-1:0] command);
        begin
            @(negedge clk);
            s_cmd = command;
            s_cmd_valid = 1;
            do @(posedge clk); while (!s_cmd_ready);
            @(negedge clk);
            s_cmd_valid = 0;
        end
    endtask

    // Hold one wire beat with its keep mask until RX accepts it.
    task automatic send_raw_beat(input logic [63:0] word_data, input logic [7:0] keep);
        begin
            @(negedge clk);
            s_rx_valid = 1;
            s_rx_data = word_data;
            s_rx_keep = keep;
            do @(posedge clk); while (!s_rx_ready);
            @(negedge clk);
            s_rx_valid = 0;
        end
    endtask

    // Fragment a byte stream into accepted RX beats for a frame sequence.
    task automatic send_byte_group(
        input logic [RAW_BITS-1:0] raw,
        input integer total,
        input integer beat_max
    );
        logic [63:0] word_data;
        logic [7:0] keep;
        integer cursor, chosen;
        begin
            if (total < 1 || total * 8 > RAW_BITS || beat_max < 1 || beat_max > 8)
                $fatal(1, "malformed input vector geometry");
            cursor = 0;
            while (cursor < total) begin
                fragments = advance(fragments);
                chosen = 1 + int'(fragments[15:0]) % beat_max;
                if (chosen > total - cursor) chosen = total - cursor;
                // Unselected high lanes must be ignored, even when nonzero.
                word_data = {~fragments, fragments};
                keep = 0;
                for (integer lane = 0; lane < 8; lane = lane + 1) begin
                    if (lane < chosen) begin
                        word_data[lane*8 +: 8] = raw[(cursor + lane)*8 +: 8];
                        keep[lane] = 1'b1;
                    end
                end
                if (fragments[19:16] < 6) @(negedge clk);
                send_raw_beat(word_data, keep);
                cursor = cursor + chosen;
            end
        end
    endtask

    // Advance the fixture's heartbeat timer by the requested number of ticks.
    task automatic pulse_ticks(input integer count);
        begin
            for (integer i = 0; i < count; i = i + 1) begin
                @(negedge clk);
                time_tick = 1;
                @(negedge clk);
                time_tick = 0;
                repeat (12) @(negedge clk);
            end
        end
    endtask

    // Drive local queries while ensuring a due heartbeat still gets transmitted.
    task automatic query_during_heartbeat(
        input logic [CMD_BITS-1:0] command, input integer ticks
    );
        integer tx_before;
        begin
            tx_before = tx_seen;
            hold_notices = 1;
            send_command(command);
            fork
                send_command(command);
                begin
                    pulse_ticks(ticks);
                    repeat (32) @(negedge clk);
                    if (tx_seen != tx_before + 1 || !m_notice_valid ||
                        !s_cmd_valid || s_cmd_ready)
                        $fatal(1, "heartbeat missed its deadline while a local query held");
                    hold_notices = 0;
                end
            join
        end
    endtask

    // Pulse the enable-to-kill control at a defined edge.
    task automatic kill_once;
        begin
            @(negedge clk);
            kill_req = 1;
            @(negedge clk);
            kill_req = 0;
        end
    endtask

    // Request a logout and wait for the control input handshake.
    task automatic logout_once;
        begin
            @(negedge clk);
            s_logout_valid = 1;
            do @(posedge clk); while (!s_logout_ready);
            @(negedge clk);
            s_logout_valid = 0;
        end
    endtask

    // Apply transmit backpressure so a partial frame stays observable.
    task automatic throttle_tx(input integer beats);
        begin
            @(negedge clk);
            tx_allowance = beats < 0 ? -1 : tx_beat_count + beats;
        end
    endtask

    // Wait until the host has observed a prefix of an incomplete TX frame.
    task automatic wait_for_partial_tx;
        integer waited;
        begin
            waited = 0;
            while (tx_beat_count < tx_allowance || !m_tx_valid) begin
                @(negedge clk);
                waited = waited + 1;
                if (waited > 100) $fatal(1, "did not reach partial transmit boundary");
            end
            if (m_tx_ready || !observed_packet_length)
                $fatal(1, "partial transmit boundary lacked blocked bytes");
        end
    endtask

    integer stimulus_index = 0;
    integer barriers = 0;
    // Compare stable checkpoint and order-slot state at a scenario barrier.
    task automatic inspect_barrier(
        input integer expected_tx_count, expected_notice_count,
        input logic [RAW_BITS-1:0] packed_expectation
    );
        integer waited;
        logic [SNAPSHOT_BITS-1:0] expected_snapshot;
        begin
            waited = 0;
            query_slot = packed_expectation[SNAPSHOT_BITS +: 8];
            expected_snapshot = packed_expectation[SNAPSHOT_BITS-1:0];
            do begin
                @(negedge clk);
                waited = waited + 1;
                if (waited > 1024)
                    $fatal(1, "barrier %0d wait exhausted at stimulus %0d, packets %0d/%0d notices %0d/%0d",
                           barriers, stimulus_index, tx_seen, expected_tx_count,
                           notices_seen, expected_notice_count);
            end while (tx_seen < expected_tx_count || notices_seen < expected_notice_count);
            // Silent debug/heartbeat packets can be behind the last counted notice.
            repeat (14) @(negedge clk);
            if (tx_seen != expected_tx_count || notices_seen != expected_notice_count)
                $fatal(1, "unexpected output crossed barrier %0d", barriers);
            if (snapshot !== expected_snapshot) begin
                $display("phase=%0d fault=%0d seq=%0d market=%b live=%0d pending=%0d",
                         session_state, last_fault, next_sequence, market_open, live_count, pending_count);
                $fatal(1, "snapshot %0d at stimulus %0d query slot %0d expected %h got %h",
                       barriers, stimulus_index, query_slot, expected_snapshot, snapshot);
            end
            barriers = barriers + 1;
        end
    endtask

    integer action, arg_a, arg_b, input_columns;
    logic [RAW_BITS-1:0] value;
    logic [63:0] trailing;
    string directory;
    localparam integer ARCHIVE_WORDS = 5 + ($bits(checkpoint_meta_t)+31)/32 +
                                     ORDER_SLOTS*(($bits(order_state_t)+31)/32);
    logic [31:0] exported_archive [0:ARCHIVE_WORDS-1];
    integer checkpoint_results = 0, archive_stalls = 0;
    bit archive_saved = 0, archive_held = 0;
    logic [32:0] archive_payload;

    always @(posedge clk) begin
        if (rst)
            archive_held <= 0;
        else begin
            if (archive_held && (!cp_out_valid || {cp_out_last,cp_out_data} !== archive_payload))
                $fatal(1,"checkpoint export changed held data");
            archive_held <= cp_out_valid && !cp_out_ready;
            archive_payload <= {cp_out_last,cp_out_data};
            if (cp_out_valid && !cp_out_ready)
                archive_stalls++;
            if (RECOVERABLE && !boot_open && (live_count != 0 || highest_order_ref != 0 || next_sequence != 1))
                $fatal(1,"uncommitted checkpoint state leaked");
            if (RECOVERABLE && checkpoint_busy && (s_login_ready || s_cmd_ready || s_rx_ready || m_tx_valid))
                $fatal(1,"session symbols accepted during checkpoint maintenance");
        end
    end

    task automatic checkpoint_begin(input logic [1:0] operation, input logic [63:0] identifier);
        @(negedge clk);
        cp_cmd_op = operation;
        cp_image_id = identifier;
        cp_cmd_valid = 1;
        do @(posedge clk); while (!cp_cmd_ready);
        @(negedge clk);
        cp_cmd_valid = 0;
    endtask

    task automatic checkpoint_end(input integer status, input logic [7:0] bad_slot);
        logic [77:0] held;
        while (!cp_result_valid) @(negedge clk);
        if (cp_result_status !== 4'(status) || cp_bad_slot !== bad_slot ||
            cp_result_op !== cp_cmd_op || cp_result_id !== cp_image_id)
            $fatal(1,"checkpoint outcome expected %0d/%0d got %0d/%0d",
                   status,bad_slot,cp_result_status,cp_bad_slot);
        held = {cp_result_status,cp_result_op,cp_result_id,cp_bad_slot};
        repeat (3) begin
            @(negedge clk);
            if (!cp_result_valid || {cp_result_status,cp_result_op,cp_result_id,cp_bad_slot} !== held)
                $fatal(1,"checkpoint result changed while stalled");
        end
        cp_result_ready = 1;
        checkpoint_results++;
        @(negedge clk);
        cp_result_ready = 0;
    endtask

    task automatic checkpoint_export(input logic [63:0] identifier, input integer expected_status);
        integer file_handle, sent, tries;
        checkpoint_begin(0,identifier);
        if (expected_status == 0) begin
            file_handle = $fopen({directory,"/hardware_archive.hex"},"w");
            if (!file_handle)
                $fatal(1,"could not open archive output");
            sent = 0;
            tries = 0;
            while (sent < ARCHIVE_WORDS) begin
                cp_out_ready = tries % 5 >= 2;
                tries++;
                @(posedge clk);
                if (cp_out_valid && cp_out_ready) begin
                    if (cp_out_last !== (sent == ARCHIVE_WORDS-1))
                        $fatal(1,"unexpected archive last at word %0d",sent);
                    exported_archive[sent] = cp_out_data;
                    $fdisplay(file_handle,"%08h",cp_out_data);
                    sent++;
                end
                @(negedge clk);
            end
            cp_out_ready = 0;
            archive_saved = 1;
            $fclose(file_handle);
        end
        checkpoint_end(expected_status,8'hff);
    endtask

    task automatic checkpoint_word(input logic [31:0] value_word, input bit last_word);
        cp_in_valid = 1;
        cp_in_data = value_word;
        cp_in_last = last_word;
        do @(posedge clk); while (!cp_in_ready);
        @(negedge clk);
        cp_in_valid = 0;
    endtask

    task automatic checkpoint_import(
        input integer variant, input integer expected_status,
        input logic [63:0] identifier, input logic [7:0] bad_slot
    );
        integer file_handle, count, final_word;
        logic [31:0] value_word;
        checkpoint_begin(1,identifier);
        if (variant == 0 || variant == -1 || variant == -3) begin
            if (!archive_saved)
                $fatal(1,"no RTL-produced archive to restore");
            final_word = variant == 0 ? ARCHIVE_WORDS : 12;
            for (integer i = 0; i < final_word; i++)
                checkpoint_word(exported_archive[i],variant == 0 && i == ARCHIVE_WORDS-1);
            if (variant == -1) begin
                cp_abort_import = 1;
                @(negedge clk);
                cp_abort_import = 0;
            end
        end else if (variant > 0) begin
            file_handle = $fopen($sformatf("%s/import-%0d.hex",directory,variant),"r");
            if (!file_handle || $fscanf(file_handle,"%d",count) != 1 || count < 1)
                $fatal(1,"missing archive import fixture %0d",variant);
            for (integer i = 0; i < count; i++) begin
                if ($fscanf(file_handle,"%h",value_word) != 1)
                    $fatal(1,"truncated host archive fixture");
                checkpoint_word(value_word,i == count-1);
            end
            $fclose(file_handle);
        end
        if (variant == -3)
            reset_core();
        else
            checkpoint_end(expected_status,bad_slot);
    endtask

    // Save checked simulation pins when requested by the waveform target.
    initial begin : waveform_capture
        string waves_path;
        if ($value$plusargs("waves=%s", waves_path)) begin
            $dumpfile(waves_path);
            $dumpvars(1, tb_ouch_order_session);
        end
    end

    initial begin
        if (!$value$plusargs("vectors=%s", directory))
            $fatal(1, "provide +vectors=DIRECTORY");
        if ($value$plusargs("stall_seed=%d", stall_seed)) begin
            stalls = 32'(stall_seed);
            fragments = 32'(stall_seed) ^ 32'h0d91_d3a7;
        end
        stim_file = $fopen({directory, "/stimulus.txt"}, "r");
        notice_file = $fopen({directory, "/notice.txt"}, "r");
        tx_file = $fopen({directory, "/tx.txt"}, "r");
        aborted_file = $fopen({directory, "/aborted.txt"}, "r");
        capture_file = $fopen({directory, "/hardware_tx.hex"}, "w");
        if (!stim_file || !notice_file || !tx_file || !capture_file || !aborted_file)
            $fatal(1, "missing trace files");
        rst = 1;
        transport_up = 0;
        time_tick = 0;
        kill_req = 0;
        s_login_valid = 0;
        s_logout_valid = 0;
        s_cmd_valid = 0;
        s_rx_valid = 0;
        s_login = '0;
        s_cmd = '0;
        s_rx_data = '0;
        s_rx_keep = '0;
        query_slot = 0;
        random_tx_ready = 0;
        m_notice_ready = 0;
        observed_packet = '0;
        repeat (3) @(negedge clk);
        rst = 0;

        input_columns = $fscanf(stim_file, "%d %d %d %h", action, arg_a, arg_b, value);
        while (input_columns == 4) begin
            case (action)
                0: reset_core();
                1: set_transport(arg_a);
                2: send_login(value[LOGIN_BITS-1:0]);
                3: send_command(value[CMD_BITS-1:0]);
                4: send_byte_group(value, arg_a, arg_b);
                5: pulse_ticks(arg_a);
                6: kill_once();
                7: logout_once();
                8: send_raw_beat(value[63:0], arg_a[7:0]);
                9: inspect_barrier(arg_a, arg_b, value);
                10: throttle_tx(arg_a);
                11: wait_for_partial_tx();
                12: query_during_heartbeat(value[CMD_BITS-1:0], arg_a);
                13: begin
                    checkpoint_begin(2,value[63:0]);
                    checkpoint_end(arg_a,8'hff);
                end
                14: checkpoint_export(value[63:0],arg_a);
                15: checkpoint_import(arg_a,arg_b,value[63:0],value[71:64]);
                16: begin
                    if (boot_open || s_login_ready || s_cmd_ready || s_rx_ready || m_tx_valid ||
                        highest_order_ref != 0 || pending_count != 0 || live_count != 0 || next_sequence != 1)
                        $fatal(1,"cold gate opened or leaked partially restored state");
                end
                default: $fatal(1, "unknown action %0d", action);
            endcase
            stimulus_index = stimulus_index + 1;
            input_columns = $fscanf(stim_file, "%d %d %d %h", action, arg_a, arg_b, value);
        end
        if (input_columns > 0 || !$feof(stim_file))
            $fatal(1, "extra token after input stream");
        input_columns = $fscanf(tx_file, "%d", trailing);
        if (input_columns > 0 || !$feof(tx_file))
            $fatal(1, "unconsumed expected TX packet");
        input_columns = $fscanf(notice_file, "%h", trailing);
        if (input_columns > 0 || !$feof(notice_file))
            $fatal(1, "unconsumed expected notice");
        input_columns = $fscanf(aborted_file, "%d", trailing);
        if (input_columns > 0 || !$feof(aborted_file) || (!RECOVERABLE && checked_aborts != 2))
            $fatal(1, "unconsumed expected partial TX prefix");
        if (m_tx_valid || m_notice_valid ||
            (!RECOVERABLE && (!tx_stalls || !notice_stalls || !receive_stalls)) ||
            (RECOVERABLE && (!archive_stalls || checkpoint_results < 5)) ||
            observed_packet_length != 0)
            $fatal(1, "test did not drain or missed a stalled boundary");
        $display("PASS OUCH session slots=%0d: %0d exact TX packets, %0d notices, %0d state barriers, %0d input beats",
                 ORDER_SLOTS, tx_seen, notices_seen, barriers, accepted_beats);
        $display("Stalls RX=%0d TX=%0d notices=%0d", receive_stalls, tx_stalls, notice_stalls);
        $display("Verified %0d exact prefixes cancelled by the transport boundary", checked_aborts);
        $fclose(stim_file);
        $fclose(tx_file);
        $fclose(notice_file);
        $fclose(capture_file);
        $fclose(aborted_file);
        $finish;
    end

    initial begin
        #25000000;
        $fatal(1, "session watchdog: stimulus %0d, packets %0d notices %0d",
               stimulus_index, tx_seen, notices_seen);
    end
endmodule

`default_nettype wire
