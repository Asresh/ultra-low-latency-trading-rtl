`timescale 1ns/1ps
`default_nettype none

// Transactionally publish a validated snapshot, configure the receiver and
// release only contiguous next-epoch deltas for the selected book slot.
module mold_snapshot_cutover #(
    parameter integer SLOTS = 3,
    parameter integer EPOCH_W = 16,
    parameter integer WAIT_W = 16,
    parameter integer MAX_ROWS = 16,
    parameter integer DELTAS = 8,
    localparam integer KEY_W = $clog2(SLOTS+1),
    localparam integer COUNT_W = $clog2(MAX_ROWS+2),
    localparam integer OCC_W = $clog2(DELTAS+1)
) (
    input wire clk, rst,

    input wire begin_valid,
    output wire begin_ready,
    input wire [31:0] begin_id,
    input wire [KEY_W-1:0] begin_slot,
    input wire [EPOCH_W-1:0] begin_expected_epoch,
    input wire [79:0] begin_session,
    input wire [63:0] begin_sequence,
    input wire [COUNT_W-1:0] begin_rows,
    input wire [31:0] begin_crc,
    input wire begin_force,
    input wire [WAIT_W-1:0] begin_spacing,

    input wire row_valid,
    output wire row_ready,
    input wire [31:0] row_id,
    input wire [127:0] row_word,
    input wire row_last,

    output wire restore_valid,
    input wire restore_ready,
    output wire [1:0] restore_kind,
    output wire [31:0] restore_id,
    output wire [KEY_W-1:0] restore_slot,
    output wire [EPOCH_W-1:0] restore_epoch,
    output wire [63:0] restore_sequence,
    output wire [COUNT_W-1:0] restore_count, restore_index,
    output wire [31:0] restore_crc,
    output wire [127:0] restore_word,

    input wire prepared_valid,
    output wire prepared_ready,
    input wire [31:0] prepared_id,
    input wire prepared_ok,

    output wire cfg_valid,
    input wire cfg_ready,
    output wire [KEY_W-1:0] cfg_slot,
    output wire [EPOCH_W-1:0] cfg_expected_epoch,
    output wire cfg_enable, cfg_force,
    output wire [79:0] cfg_session,
    output wire [63:0] cfg_sequence,
    output wire [WAIT_W-1:0] cfg_request_spacing,

    input wire ack_valid,
    output wire ack_ready,
    input wire [1:0] ack_status,
    input wire [KEY_W-1:0] ack_slot,
    input wire [EPOCH_W-1:0] ack_epoch,
    input wire ack_active,

    input wire s_valid,
    output wire s_ready,
    input wire [KEY_W-1:0] s_slot,
    input wire [EPOCH_W-1:0] s_epoch,
    input wire [587:0] s_data,
    output wire m_valid,
    input wire m_ready,
    output wire [KEY_W-1:0] m_slot,
    output wire [EPOCH_W-1:0] m_epoch,
    output wire [587:0] m_data,

    output wire done_valid,
    input wire done_ready,
    output logic [31:0] done_id,
    output logic [KEY_W-1:0] done_slot,
    output logic [3:0] done_status,
    output logic [EPOCH_W-1:0] done_native_epoch,
    output logic done_native_installed, done_book_committed,
    output logic [OCC_W-1:0] done_buffered,
    output logic [31:0] done_actual_crc,

    input wire [KEY_W-1:0] monitor_slot,
    output wire monitor_valid, monitor_known, monitor_quarantined,
    output wire [EPOCH_W-1:0] monitor_epoch,
    output wire [63:0] monitor_sequence,
    output wire [31:0] monitor_stale, monitor_gap, monitor_delivered, monitor_buffered,
    output wire [31:0] invalid_slots,
    output wire [3:0] phase,
    output wire scope_gap,
    output wire [OCC_W-1:0] delta_used,
    output wire [63:0] candidate_sequence,
    output logic [31:0] foreign_rows, foreign_prepares, committed_books, failed_cutovers
);
    typedef enum logic [3:0] {
        IDLE, UPLOAD, HEADER, ROWS, PREPARE, FENCE, CONFIGURE, WAIT_ACK, FINALIZE, DRAIN
    } phase_t;
    localparam logic [1:0] BOOK_HEADER = 0, BOOK_ROW = 1, BOOK_COMMIT = 2, BOOK_ABORT = 3;
    localparam logic [3:0] OK = 0, BAD_SLOT = 1, BAD_ID = 2, BAD_EPOCH = 3,
                           BAD_COUNT = 4, BAD_CRC = 5, BOOK_REJECTED = 6,
                           RECEIVER_REJECTED = 7, DELTA_GAP = 8;

    localparam integer ROW_W = MAX_ROWS > 1 ? $clog2(MAX_ROWS) : 1;
    phase_t state;
    logic [31:0] highest_id, identity, expected_crc, accumulating, captured_crc;
    logic [KEY_W-1:0] selected_slot;
    logic [EPOCH_W-1:0] old_epoch, current_native_epoch;
    logic [79:0] selected_session;
    logic [63:0] first_sequence;
    logic [COUNT_W-1:0] promised_rows, uploaded_rows, read_index;
    logic [127:0] snapshot [0:MAX_ROWS-1];
    logic force_cut, fence_active, native_installed, final_commit, completion_pending;
    logic [WAIT_W-1:0] spacing;
    logic [3:0] control_status, final_status;

    wire [EPOCH_W-1:0] next_epoch = old_epoch + 1'b1;
    wire begin_fire = begin_valid && begin_ready;
    wire begin_bad = begin_fire && control_status != OK;
    wire row_fire = row_valid && row_ready;
    wire row_ours = row_fire && row_id == identity;
    wire [COUNT_W-1:0] count_next = uploaded_rows == COUNT_W'(MAX_ROWS+1) ?
                                    uploaded_rows : uploaded_rows + 1'b1;
    wire [31:0] row_crc = crc_row(accumulating,row_word);
    wire [3:0] row_status = count_next != promised_rows ? BAD_COUNT :
                            ~row_crc != expected_crc ? BAD_CRC : OK;
    wire upload_bad = row_ours && row_last && row_status != OK;
    wire restore_fire = restore_valid && restore_ready;
    wire prepare_fire = prepared_valid && prepared_ready;
    wire our_prepare = prepare_fire && prepared_id == identity;
    wire start_fence = our_prepare && prepared_ok;
    wire scope_accept = state == WAIT_ACK || (state == FINALIZE && final_commit);
    wire old_output_held, gap_at_cut, draining;
    wire [OCC_W-1:0] delta_at_cut;
    wire config_fire = cfg_valid && cfg_ready;
    wire ack_fire = ack_valid && ack_ready;
    wire ack_matches = ack_slot == selected_slot;
    wire ack_success = ack_status == 0 && ack_matches && ack_epoch == next_epoch && ack_active;
    wire finalized = state == FINALIZE && restore_fire;
    wire release_deltas = finalized && final_commit && !gap_at_cut;

    assign phase = state;
    assign begin_ready = !rst && state == IDLE && (!completion_pending || done_ready);
    assign row_ready = !rst && state == UPLOAD;
    assign prepared_ready = !rst && state == PREPARE;
    assign cfg_valid = !rst && state == CONFIGURE;
    assign ack_ready = !rst && state == WAIT_ACK;
    assign restore_valid = !rst && (state == HEADER || state == ROWS || state == FINALIZE);
    assign done_valid = !rst && completion_pending;

    assign cfg_enable = 1;
    assign cfg_expected_epoch = old_epoch;
    assign cfg_force = force_cut;
    assign cfg_session = selected_session;
    assign cfg_slot = selected_slot;
    assign cfg_sequence = first_sequence;
    assign cfg_request_spacing = spacing;

    assign restore_kind = state == HEADER ? BOOK_HEADER : state == ROWS ? BOOK_ROW :
                          final_commit ? BOOK_COMMIT : BOOK_ABORT;
    assign restore_id = identity;
    assign restore_slot = selected_slot;
    assign restore_epoch = next_epoch;
    assign restore_sequence = first_sequence;
    assign restore_count = promised_rows;
    assign restore_index = state == ROWS ? read_index : '0;
    assign restore_crc = expected_crc;
    assign restore_word = state == ROWS ? snapshot[ROW_W'(read_index)] : '0;

    always_comb begin
        control_status = OK;
        if (int'(begin_slot) >= SLOTS)
            control_status = BAD_SLOT;
        else if (begin_id <= highest_id)
            control_status = BAD_ID;
        else if (&begin_expected_epoch)
            control_status = BAD_EPOCH;
        else if (begin_rows > COUNT_W'(MAX_ROWS))
            control_status = BAD_COUNT;
        else if (begin_rows == 0 && begin_crc != 0)
            control_status = BAD_CRC;
    end

    function automatic logic [31:0] crc_row(input logic [31:0] previous, input logic [127:0] row);
        logic [31:0] update;
        begin
            update = previous;
            for (integer lane = 0; lane < 16; lane++) begin
                update = update ^ {24'd0,row[8*lane+:8]};
                for (integer bit_index = 0; bit_index < 8; bit_index++)
                    update = (update >> 1) ^ (update[0] ? 32'hedb88320 : 32'd0);
            end
            crc_row = update;
        end
    endfunction

    function automatic logic [31:0] inc(input logic [31:0] count);
        inc = (&count) ? count : count + 1'b1;
    endfunction

    mold_snapshot_router #(.SLOTS(SLOTS), .EPOCH_W(EPOCH_W), .DELTAS(DELTAS)) router (
        .clk, .rst, .scope_start(start_fence), .scope_fenced(fence_active), .scope_accept,
        .scope_finish(finalized && fence_active), .scope_commit(release_deltas),
        .scope_slot(selected_slot), .scope_epoch(next_epoch), .scope_sequence(first_sequence),
        .native_observed(ack_fire && ack_matches), .observed_epoch(ack_epoch),
        .old_output_held, .scope_gap, .gap_at_cut, .candidate_sequence, .delta_used, .delta_at_cut, .draining,
        .s_valid, .s_ready, .s_slot, .s_epoch, .s_data, .m_valid, .m_ready, .m_slot, .m_epoch, .m_data,
        .monitor_slot, .monitor_valid, .monitor_known, .monitor_quarantined, .monitor_epoch,
        .monitor_sequence, .monitor_stale, .monitor_gap, .monitor_delivered, .monitor_buffered, .invalid_slots
    );

    always_ff @(posedge clk) begin
        if (row_ours && uploaded_rows < COUNT_W'(MAX_ROWS))
            snapshot[ROW_W'(uploaded_rows)] <= row_word;
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= IDLE;
            highest_id <= 0;
            identity <= 0;
            selected_slot <= 0;
            old_epoch <= 0;
            current_native_epoch <= 0;
            selected_session <= 0;
            first_sequence <= 0;
            promised_rows <= 0;
            uploaded_rows <= 0;
            read_index <= 0;
            expected_crc <= 0;
            captured_crc <= 0;
            accumulating <= '1;
            force_cut <= 0;
            spacing <= 0;
            fence_active <= 0;
            native_installed <= 0;
            final_commit <= 0;
            final_status <= OK;
        end else begin
            case (state)
                IDLE: if (begin_fire && !begin_bad) begin
                    highest_id <= begin_id;
                    identity <= begin_id;
                    selected_slot <= begin_slot;
                    old_epoch <= begin_expected_epoch;
                    current_native_epoch <= begin_expected_epoch;
                    selected_session <= begin_session;
                    first_sequence <= begin_sequence;
                    promised_rows <= begin_rows;
                    uploaded_rows <= 0;
                    expected_crc <= begin_crc;
                    captured_crc <= 0;
                    accumulating <= '1;
                    force_cut <= begin_force;
                    spacing <= begin_spacing;
                    native_installed <= 0;
                    final_commit <= 0;
                    final_status <= OK;
                    if (begin_rows == 0) state <= HEADER;
                    else state <= UPLOAD;
                end
                UPLOAD: if (row_ours) begin
                    uploaded_rows <= count_next;
                    accumulating <= row_crc;
                    if (row_last) begin
                        captured_crc <= ~row_crc;
                        if (upload_bad) state <= IDLE;
                        else state <= HEADER;
                    end
                end
                HEADER: if (restore_fire) begin
                    read_index <= 0;
                    if (promised_rows == 0) state <= PREPARE;
                    else state <= ROWS;
                end
                ROWS: if (restore_fire) begin
                    if (read_index + 1'b1 == promised_rows)
                        state <= PREPARE;
                    else
                        read_index <= read_index + 1'b1;
                end
                PREPARE: if (our_prepare) begin
                    if (prepared_ok) begin
                        fence_active <= 1;
                        state <= FENCE;
                    end else begin
                        final_status <= BOOK_REJECTED;
                        state <= FINALIZE;
                    end
                end
                FENCE: if (!old_output_held)
                    state <= CONFIGURE;
                CONFIGURE: if (config_fire)
                    state <= WAIT_ACK;
                WAIT_ACK: if (ack_fire) begin
                    if (ack_matches)
                        current_native_epoch <= ack_epoch;
                    native_installed <= ack_success;
                    final_commit <= ack_success && !gap_at_cut;
                    final_status <= !ack_success ? RECEIVER_REJECTED : gap_at_cut ? DELTA_GAP : OK;
                    state <= FINALIZE;
                end
                FINALIZE: if (restore_fire) begin
                    fence_active <= 0;
                    if (release_deltas) state <= DRAIN;
                    else state <= IDLE;
                end
                DRAIN: if (!draining)
                    state <= IDLE;
                default: state <= IDLE;
            endcase
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            completion_pending <= 0;
            done_id <= 0;
            done_slot <= 0;
            done_status <= OK;
            done_native_epoch <= 0;
            done_native_installed <= 0;
            done_book_committed <= 0;
            done_buffered <= 0;
            done_actual_crc <= 0;
            foreign_rows <= 0;
            foreign_prepares <= 0;
            committed_books <= 0;
            failed_cutovers <= 0;
        end else begin
            if (done_ready)
                completion_pending <= 0;
            if (row_fire && !row_ours)
                foreign_rows <= inc(foreign_rows);
            if (prepare_fire && !our_prepare)
                foreign_prepares <= inc(foreign_prepares);
            if (finalized && final_commit)
                committed_books <= inc(committed_books);
            if (begin_bad || upload_bad || (finalized && !release_deltas))
                failed_cutovers <= inc(failed_cutovers);

            if (begin_bad || upload_bad || finalized) begin
                completion_pending <= 1;
                done_id <= begin_bad ? begin_id : identity;
                done_slot <= begin_bad ? begin_slot : selected_slot;
                done_status <= begin_bad ? control_status : upload_bad ? row_status :
                               final_commit && gap_at_cut ? DELTA_GAP : final_status;
                done_native_epoch <= begin_bad ? begin_expected_epoch : current_native_epoch;
                done_native_installed <= finalized && native_installed;
                done_book_committed <= finalized && final_commit;
                done_buffered <= finalized ? delta_at_cut : '0;
                done_actual_crc <= begin_bad ? '0 : upload_bad ? ~row_crc : captured_crc;
            end
        end
    end
endmodule

`default_nettype wire
