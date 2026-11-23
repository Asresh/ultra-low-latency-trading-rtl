`timescale 1ns/1ps

module redundant_market_data #(
    parameter integer PACKET_BYTES = 1536,
    parameter integer MESSAGE_BYTES = 64,
    parameter integer BLOCKS = 64,
    parameter integer WINDOW = 16,
    parameter integer GAP_TICKS = 64,
    parameter integer REQUEST_MAX = 32
) (
    input  logic         clk,
    input  logic         rst,

    input  logic         cfg_valid,
    output logic         cfg_ready,
    input  logic [79:0]  cfg_session,
    input  logic [63:0]  cfg_sequence,

    input  logic [1:0]   feed_valid,
    output logic [1:0]   feed_ready,
    input  logic [127:0] feed_data,
    input  logic [15:0]  feed_keep,
    input  logic [1:0]   feed_first,
    input  logic [1:0]   feed_last,

    output logic         event_valid,
    input  logic         event_ready,
    output logic [587:0] event_data,

    output logic         request_valid,
    input  logic         request_ready,
    output logic [159:0] request_data,

    output logic [63:0]  expected_sequence,
    output logic [63:0]  high_water,
    output logic         gap_pending,
    output logic         session_end_seen,
    output logic [5:0]   reorder_occupancy,

    input  logic         stats_snapshot,
    input  logic [3:0]   stats_index,
    output logic [31:0]  stats_data,
    output logic [5:0]   stats_peak
);
    localparam integer CANDIDATE_BITS = 83 + 8*MESSAGE_BYTES;
    localparam logic [2:0] DATA = 3'd0, HEARTBEAT = 3'd1, END_SESSION = 3'd2,
                           BAD_PACKET = 3'd3, BOUND = 3'd4, OTHER_SESSION = 3'd5;

    logic control_flush, clear, session_active;
    logic [79:0] session_id;
    logic [63:0] start_sequence;
    logic seed_pending, seed_ready;
    logic [3:0] bank_valid, bank_ready;
    logic [4*CANDIDATE_BITS-1:0] bank_data;
    logic candidate_valid, candidate_ready, candidate_taken;
    logic candidate_lane, unused_bank_number;
    logic [2:0] candidate_kind;
    logic [63:0] candidate_sequence, merged_sequence, request_sequence;
    logic [15:0] candidate_length, merged_length, request_count;
    logic [MESSAGE_BYTES*8-1:0] candidate_payload, merged_payload;
    logic merged_valid, merged_ready;
    logic beat_valid, beat_ready, beat_first, beat_last;
    logic [63:0] beat_data, beat_sequence;
    logic [7:0] beat_keep;
    logic decoded_valid, decoded_taken, reorder_request_valid;
    logic [15:0] count_inc;

    assign cfg_ready = !rst;
    assign control_flush = cfg_valid && cfg_ready;
    assign clear = rst || control_flush;
    assign request_valid = !clear && reorder_request_valid;
    assign request_data = {session_id, request_sequence, request_count};
    assign event_valid = !clear && decoded_valid;
    assign decoded_taken = event_valid && event_ready;
    assign candidate_taken = candidate_valid && candidate_ready;

    always_ff @(posedge clk) begin
        if (rst) begin
            session_active <= 1'b0;
            session_id     <= '0;
            start_sequence <= '0;
            seed_pending   <= 1'b0;
        end else if (control_flush) begin
            session_active <= 1'b1;
            session_id     <= cfg_session;
            start_sequence <= cfg_sequence;
            seed_pending   <= 1'b1;
        end else if (seed_pending && seed_ready) begin
            seed_pending <= 1'b0;
        end
    end

    for (genvar lane = 0; lane < 2; lane = lane + 1) begin : g_feed
        mold_feed #(
            .PACKET_BYTES(PACKET_BYTES), .MESSAGE_BYTES(MESSAGE_BYTES), .BLOCKS(BLOCKS)
        ) capture (
            .clk, .clear, .session_active, .session_id,
            .rx_valid(feed_valid[lane]), .rx_ready(feed_ready[lane]),
            .rx_data(feed_data[64*lane +: 64]), .rx_keep(feed_keep[8*lane +: 8]),
            .rx_first(feed_first[lane]), .rx_last(feed_last[lane]),
            .bank_valid(bank_valid[2*lane +: 2]), .bank_ready(bank_ready[2*lane +: 2]),
            .bank_data(bank_data[2*CANDIDATE_BITS*lane +: 2*CANDIDATE_BITS])
        );
    end

    mold_rr_mux #(.WIDTH(CANDIDATE_BITS)) candidate_mux (
        .clk, .clear, .s_valid(bank_valid), .s_ready(bank_ready), .s_data(bank_data),
        .m_valid(candidate_valid), .m_ready(candidate_ready), .m_index({candidate_lane, unused_bank_number}),
        .m_data({candidate_kind, candidate_sequence, candidate_length, candidate_payload})
    );

    mold_reorder #(
        .MESSAGE_BYTES(MESSAGE_BYTES), .WINDOW(WINDOW), .GAP_TICKS(GAP_TICKS),
        .REQUEST_MAX(REQUEST_MAX)
    ) reorder (
        .clk, .clear, .session_active, .seed_sequence(cfg_sequence),
        .s_valid(candidate_valid), .s_ready(candidate_ready), .s_kind(candidate_kind),
        .s_sequence(candidate_sequence), .s_length(candidate_length), .s_payload(candidate_payload),
        .m_valid(merged_valid), .m_ready(merged_ready), .m_sequence(merged_sequence),
        .m_length(merged_length), .m_payload(merged_payload),
        .request_valid(reorder_request_valid), .request_ready(request_ready && !clear),
        .request_sequence, .request_count,
        .expected_sequence, .high_water, .gap_pending, .session_end_seen,
        .occupancy(reorder_occupancy),
        .hit_store(count_inc[0]), .hit_duplicate(count_inc[1]), .hit_conflict(count_inc[2]),
        .hit_stale(count_inc[3]), .hit_far(count_inc[4])
    );

    itch_segmenter #(.MESSAGE_BYTES(MESSAGE_BYTES)) segmenter (
        .clk, .clear,
        .s_valid(merged_valid), .s_ready(merged_ready), .s_sequence(merged_sequence),
        .s_length(merged_length), .s_payload(merged_payload),
        .m_valid(beat_valid), .m_ready(beat_ready), .m_data(beat_data), .m_keep(beat_keep),
        .m_first(beat_first), .m_last(beat_last), .m_sequence(beat_sequence)
    );

    itch_decode decoder (
        .clk, .rst(clear),
        .s_valid(beat_valid), .s_ready(beat_ready), .s_data(beat_data), .s_keep(beat_keep),
        .s_first(beat_first), .s_last(beat_last), .s_sequence(beat_sequence),
        .seed_valid(seed_pending), .seed_ready, .seed_sequence(start_sequence),
        .m_valid(decoded_valid), .m_ready(event_ready),
        .m_result(event_data[587:586]), .m_seq_status(event_data[585:584]),
        .m_sequence(event_data[583:520]), .m_expected_sequence(event_data[519:456]),
        .m_type(event_data[455:448]), .m_locate(event_data[447:432]),
        .m_tracking(event_data[431:416]), .m_timestamp(event_data[415:368]),
        .m_order_ref(event_data[367:304]), .m_new_order_ref(event_data[303:240]),
        .m_side(event_data[239:232]), .m_shares(event_data[231:200]),
        .m_stock(event_data[199:136]), .m_price(event_data[135:104]),
        .m_match(event_data[103:40]), .m_printable(event_data[39:32]),
        .m_mpid(event_data[31:0])
    );

    assign count_inc[5] = candidate_taken && candidate_kind == BAD_PACKET;
    assign count_inc[6] = candidate_taken && candidate_kind == BOUND;
    assign count_inc[7] = candidate_taken && candidate_kind == OTHER_SESSION;
    assign count_inc[8] = candidate_taken && candidate_kind == HEARTBEAT;
    assign count_inc[9] = candidate_taken && candidate_kind == END_SESSION;
    assign count_inc[10] = request_valid && request_ready && !clear;
    assign count_inc[11] = decoded_taken && event_data[587:586] == 2'd0;
    assign count_inc[12] = decoded_taken && event_data[587:586] == 2'd1;
    assign count_inc[13] = decoded_taken && event_data[587:586] == 2'd2;
    assign count_inc[14] = candidate_taken && candidate_kind == DATA && candidate_lane == 0;
    assign count_inc[15] = candidate_taken && candidate_kind == DATA && candidate_lane == 1;

    mold_statistics statistics (
        .clk, .clear, .increment(count_inc), .occupancy(reorder_occupancy),
        .snapshot(stats_snapshot), .read_index(stats_index), .read_data(stats_data),
        .peak_occupancy(stats_peak)
    );
endmodule
