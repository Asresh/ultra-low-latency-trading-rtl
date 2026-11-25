`default_nettype none

module formal_snapshot_cutover;
    localparam integer SLOTS = 2, KEY_W = 2, EPOCH_W = 3, WAIT_W = 3;
    localparam integer MAX_ROWS = 2, COUNT_W = 2, DELTAS = 2, OCC_W = 2;
    (* gclk *) logic clk;
    (* anyseq *) logic rst, begin_valid, row_valid, prepared_valid, ack_valid, s_valid;
    (* anyseq *) logic restore_ready, cfg_ready, m_ready, done_ready;
    (* anyseq *) logic [3:0] begin_key, row_key, prep_key, start_sequence, source_sequence;
    (* anyseq *) logic [KEY_W-1:0] begin_slot, ack_slot, s_slot, monitor_slot;
    (* anyseq *) logic [EPOCH_W-1:0] begin_expected_epoch, ack_epoch, s_epoch;
    (* anyseq *) logic [COUNT_W-1:0] begin_rows;
    (* anyseq *) logic [31:0] begin_crc;
    (* anyseq *) logic begin_force, row_last, prepared_ok, ack_active;
    (* anyseq *) logic [7:0] small_row, small_event;
    (* anyseq *) logic [1:0] ack_status;
    wire [31:0] begin_id = 32'(begin_key), row_id = 32'(row_key), prepared_id = 32'(prep_key);
    wire [79:0] begin_session = "BOOKSNAP01";
    wire [63:0] begin_sequence = 64'(start_sequence);
    wire [WAIT_W-1:0] begin_spacing = WAIT_W'(4);
    wire [127:0] row_word = 128'(small_row);
    wire [587:0] s_data = {4'd0,60'd0,source_sequence,512'd0,small_event};

    wire begin_ready, row_ready, prepared_ready, ack_ready, s_ready, m_valid;
    wire restore_valid, cfg_valid, done_valid, monitor_valid;
    wire [1:0] restore_kind;
    wire [31:0] restore_id, restore_crc, done_id, done_actual_crc;
    wire [KEY_W-1:0] restore_slot, cfg_slot, m_slot, done_slot;
    wire [EPOCH_W-1:0] restore_epoch, cfg_expected_epoch, m_epoch, done_native_epoch;
    wire [EPOCH_W-1:0] monitor_epoch;
    wire [63:0] restore_sequence, cfg_sequence, monitor_sequence, candidate_sequence;
    wire [COUNT_W-1:0] restore_count, restore_index;
    wire [127:0] restore_word;
    wire cfg_enable, cfg_force, done_native_installed, done_book_committed, scope_gap;
    wire [79:0] cfg_session;
    wire [WAIT_W-1:0] cfg_request_spacing;
    wire [587:0] m_data;
    wire [3:0] phase, done_status;
    wire [OCC_W-1:0] delta_used, done_buffered;
    wire monitor_known, monitor_quarantined;
    wire [31:0] monitor_stale, monitor_gap, monitor_delivered, monitor_buffered, invalid_slots;
    wire [31:0] foreign_rows, foreign_prepares, committed_books, failed_cutovers;

    mold_snapshot_cutover #(
        .SLOTS(SLOTS), .EPOCH_W(EPOCH_W), .WAIT_W(WAIT_W), .MAX_ROWS(MAX_ROWS), .DELTAS(DELTAS)
    ) dut (.*);

    logic initialized = 0;
    always @(posedge clk) begin
        initialized <= 1;
        if (!initialized)
            assume(rst);
        if (rst)
            assert(!m_valid && !cfg_valid && !restore_valid && !done_valid && !begin_ready);
        if (initialized && !$past(rst) && !rst) begin
            if ($past(m_valid && !m_ready)) begin
                assert(m_valid);
                assert($stable({m_slot,m_epoch,m_data}));
            end
            if ($past(cfg_valid && !cfg_ready)) begin
                assert(cfg_valid);
                assert($stable({cfg_slot,cfg_expected_epoch,cfg_enable,cfg_force,
                                cfg_session,cfg_sequence,cfg_request_spacing}));
            end
            if ($past(restore_valid && !restore_ready)) begin
                assert(restore_valid);
                assert($stable({restore_kind,restore_id,restore_slot,restore_epoch,restore_sequence,
                                restore_count,restore_index,restore_crc,restore_word}));
            end
            if ($past(done_valid && !done_ready)) begin
                assert(done_valid);
                assert($stable({done_id,done_slot,done_status,done_native_epoch,
                                done_native_installed,done_book_committed,done_buffered,done_actual_crc}));
            end
            assert(delta_used <= DELTAS);
            if (cfg_valid)
                assert(cfg_enable && (!m_valid || m_slot != cfg_slot));
            if (done_valid && done_book_committed)
                assert(done_native_installed && (done_status == 0 || done_status == 8));
            if (restore_valid && restore_kind == 1)
                assert(restore_index < restore_count);
            cover(done_valid && done_status == 0 && done_buffered != 0);
            cover(done_valid && done_status == 6 && !done_native_installed);
            cover(done_valid && done_status == 7 && !done_book_committed);
            cover(done_valid && done_status == 8 && done_book_committed);
        end
    end
endmodule

`default_nettype wire
