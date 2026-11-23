`timescale 1ns/1ps
// NASDAQ TotalView-ITCH 5.0 order messages on a message-aligned 64-bit stream.
module itch_decode (
    input  logic        clk,
    input  logic        rst,

    input  logic        s_valid,
    output logic        s_ready,
    input  logic [63:0] s_data,
    input  logic [7:0]  s_keep,
    input  logic        s_first,
    input  logic        s_last,
    input  logic [63:0] s_sequence,

    input  logic        seed_valid,
    output logic        seed_ready,
    input  logic [63:0] seed_sequence,

    output logic        m_valid,
    input  logic        m_ready,
    output logic [1:0]  m_result,
    output logic [1:0]  m_seq_status,
    output logic [63:0] m_sequence,
    output logic [63:0] m_expected_sequence,
    output logic [7:0]  m_type,
    output logic [15:0] m_locate,
    output logic [15:0] m_tracking,
    output logic [47:0] m_timestamp,
    output logic [63:0] m_order_ref,
    output logic [63:0] m_new_order_ref,
    output logic [7:0]  m_side,
    output logic [31:0] m_shares,
    output logic [63:0] m_stock,
    output logic [31:0] m_price,
    output logic [63:0] m_match,
    output logic [7:0]  m_printable,
    output logic [31:0] m_mpid
);
    localparam logic [1:0] DECODED = 2'd0, UNSUPPORTED = 2'd1, MALFORMED = 2'd2;
    localparam logic [1:0] IN_ORDER = 2'd0, UNSEEDED = 2'd1, GAP = 2'd2, STALE = 2'd3;

    typedef struct packed {
        logic [1:0]  result;
        logic [1:0]  seq_status;
        logic [63:0] seq;
        logic [63:0] expected;
        logic [7:0]  msg_type;
        logic [15:0] locate;
        logic [15:0] tracking;
        logic [47:0] timestamp;
        logic [63:0] order_ref;
        logic [63:0] new_order_ref;
        logic [7:0]  side;
        logic [31:0] shares;
        logic [63:0] stock;
        logic [31:0] price;
        logic [63:0] match_id;
        logic [7:0]  printable;
        logic [31:0] mpid;
    } event_t;

    // Tuple: supported, index of final beat, final keep mask.
    // Look up the exact payload length and normalized event fields for a supported ITCH type.
    function automatic logic [11:0] shape(input logic [7:0] msg_type);
        case (msg_type)
            "A": shape = {1'b1, 3'd4, 8'h0f}; // 36 bytes
            "F": shape = {1'b1, 3'd4, 8'hff}; // 40 bytes
            "E": shape = {1'b1, 3'd3, 8'h7f}; // 31 bytes
            "C": shape = {1'b1, 3'd4, 8'h0f}; // 36 bytes
            "X": shape = {1'b1, 3'd2, 8'h7f}; // 23 bytes
            "D": shape = {1'b1, 3'd2, 8'h07}; // 19 bytes
            "U": shape = {1'b1, 3'd4, 8'h07}; // 35 bytes
            default: shape = '0;
        endcase
    endfunction

    // Accept only a nonzero contiguous prefix of valid low byte lanes.
    function automatic logic prefix_keep(input logic [7:0] mask);
        case (mask)
            8'h01, 8'h03, 8'h07, 8'h0f, 8'h1f, 8'h3f, 8'h7f, 8'hff:
                prefix_keep = 1'b1;
            default: prefix_keep = 1'b0;
        endcase
    endfunction

    logic in_frame, frame_framed, frame_bad;
    logic [2:0] beat_index;
    logic [11:0] frame_shape;
    logic [1:0] frame_seq_status;
    logic [63:0] frame_sequence, frame_expected;
    logic [255:0] prefix;
    logic seq_known;
    logic [63:0] next_sequence;

    logic take, push, pop;
    logic [1:0] occupancy;
    logic read_slot, write_slot;
    event_t queue0, queue1, head, decoded;

    logic [2:0] index_now;
    logic known_type, framed_now, bad_now;
    logic [2:0] final_index;
    logic [7:0] final_keep;
    logic [1:0] seq_status_now;
    logic [63:0] seq_now, expected_now;
    logic seq_behind;
    logic [7:0] msg_type_now;
    logic [63:0] wire_beat;
    logic [311:0] message_data;

    assign m_valid = occupancy != 0;
    assign pop = m_valid && m_ready;
    assign seed_ready = !rst && !in_frame && occupancy == 0;

    // Only an end beat reserves output capacity. Ready is independent of m_ready.
    assign s_ready = !rst && !(seed_valid && seed_ready)
                           && (!s_last || occupancy != 2);
    assign take = s_valid && s_ready;
    assign push = take && s_last;

    assign head = read_slot ? queue1 : queue0;
    assign {m_result, m_seq_status, m_sequence, m_expected_sequence, m_type,
            m_locate, m_tracking, m_timestamp, m_order_ref, m_new_order_ref,
            m_side, m_shares, m_stock, m_price, m_match, m_printable, m_mpid} = head;

    // Convert lane zero (first byte) into the most significant byte of a beat.
    for (genvar lane = 0; lane < 8; lane = lane + 1) begin : g_wire_order
        assign wire_beat[63 - 8*lane -: 8] = s_data[8*lane +: 8];
    end

    assign index_now = in_frame ? beat_index : 0;
    assign framed_now = in_frame ? frame_framed : s_first;
    assign {known_type, final_index, final_keep} = in_frame ? frame_shape
                                                           : shape(s_data[7:0]);

    // The sign bit of the modular distance is the exclusive OR of both
    // high bits and the borrow from the low-bit comparison.
    assign seq_behind = s_sequence[63] ^ next_sequence[63] ^
                        (s_sequence[62:0] < next_sequence[62:0]);
    assign msg_type_now = in_frame ? prefix[255:248] : s_data[7:0];
    assign seq_now = in_frame ? frame_sequence : s_first ? s_sequence : 64'd0;
    assign expected_now = in_frame ? frame_expected
                                   : (s_first && seq_known) ? next_sequence : 64'd0;

    always @* begin
        if (in_frame)
            seq_status_now = frame_seq_status;
        else if (!s_first || !seq_known)
            seq_status_now = UNSEEDED;
        else if (s_sequence == next_sequence)
            seq_status_now = IN_ORDER;
        else if (seq_behind)
            seq_status_now = STALE;
        else
            seq_status_now = GAP;
    end

    always @* begin
        bad_now = (in_frame && frame_bad) || !framed_now || (in_frame && s_first);
        if (s_last ? !prefix_keep(s_keep) : s_keep != 8'hff)
            bad_now = 1'b1;
        if (known_type) begin
            if (s_last && (index_now != final_index || s_keep != final_keep))
                bad_now = 1'b1;
            if (!s_last && index_now >= final_index)
                bad_now = 1'b1;
        end
    end

    // The shortest supported message ends on beat two. Its first two
    // beats are always registered. Only beats two and three need a
    // bypass when they are the final beat of a supported message.
    always @* begin
        message_data = {prefix[247:0], wire_beat};
        if (beat_index == 3'd2)
            message_data[191:128] = wire_beat;
        if (beat_index == 3'd3)
            message_data[127:64] = wire_beat;
    end

    always @* begin
        decoded = '0;
        decoded.seq_status = seq_status_now;
        decoded.seq = seq_now;
        decoded.expected = expected_now;
        if (framed_now)
            decoded.msg_type = msg_type_now;

        // A/F and C always end on beat four, so these enums are already
        // in the prefix registers when any valid payload can be emitted.
        if (bad_now || ((msg_type_now == "A" || msg_type_now == "F") &&
                        prefix[255-19*8 -: 8] != "B" &&
                        prefix[255-19*8 -: 8] != "S") ||
                       (msg_type_now == "C" && prefix[7:0] != "Y" &&
                                               prefix[7:0] != "N")) begin
            decoded.result = MALFORMED;
        end else if (!known_type) begin
            decoded.result = UNSUPPORTED;
        end else begin
            decoded.result = DECODED;
            decoded.locate    = message_data[319-1*8  -: 16];
            decoded.tracking  = message_data[319-3*8  -: 16];
            decoded.timestamp = message_data[319-5*8  -: 48];
            decoded.order_ref = message_data[319-11*8 -: 64];

            case (msg_type_now)
                "A", "F": begin
                    decoded.side   = message_data[319-19*8 -: 8];
                    decoded.shares = message_data[319-20*8 -: 32];
                    decoded.stock  = message_data[319-24*8 -: 64];
                    decoded.price  = message_data[319-32*8 -: 32];
                    if (msg_type_now == "F")
                        decoded.mpid = message_data[319-36*8 -: 32];
                end
                "E", "C": begin
                    decoded.shares   = message_data[319-19*8 -: 32];
                    decoded.match_id = message_data[319-23*8 -: 64];
                    if (msg_type_now == "C") begin
                        decoded.printable = message_data[319-31*8 -: 8];
                        decoded.price     = message_data[319-32*8 -: 32];
                    end
                end
                "X": decoded.shares = message_data[319-19*8 -: 32];
                "U": begin
                    decoded.new_order_ref = message_data[319-19*8 -: 64];
                    decoded.shares        = message_data[319-27*8 -: 32];
                    decoded.price         = message_data[319-31*8 -: 32];
                end
                default: begin end // D only uses the common fields.
            endcase
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            in_frame         <= 1'b0;
            frame_framed     <= 1'b0;
            frame_bad        <= 1'b0;
            beat_index       <= '0;
            frame_shape      <= '0;
            frame_seq_status <= UNSEEDED;
            frame_sequence   <= '0;
            frame_expected   <= '0;
            seq_known        <= 1'b0;
            next_sequence    <= '0;
        end else begin
            if (seed_valid && seed_ready) begin
                seq_known     <= 1'b1;
                next_sequence <= seed_sequence;
            end
            if (take) begin
                in_frame <= !s_last;
                if (s_last) begin
                    beat_index <= '0;
                    if (framed_now && seq_status_now != STALE) begin
                        seq_known     <= 1'b1;
                        next_sequence <= seq_now + 64'd1;
                    end
                end else begin
                    // Unknown frames and overlong malformed frames drain
                    // indefinitely without wrapping the assembly index.
                    if (index_now < 3'd5)
                        beat_index <= index_now + 3'd1;
                    frame_bad <= bad_now;
                    if (!in_frame) begin
                        frame_framed     <= framed_now;
                        frame_shape      <= {known_type, final_index, final_keep};
                        frame_seq_status <= seq_status_now;
                        frame_sequence   <= seq_now;
                        frame_expected   <= expected_now;
                    end
                    case (index_now)
                        3'd0: prefix[255:192] <= wire_beat;
                        3'd1: prefix[191:128] <= wire_beat;
                        3'd2: prefix[127:64]  <= wire_beat;
                        3'd3: prefix[63:0]   <= wire_beat;
                        default: begin end
                    endcase
                end
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            occupancy  <= '0;
            read_slot  <= 1'b0;
            write_slot <= 1'b0;
        end else begin
            case ({push, pop})
                2'b10: occupancy <= occupancy + 2'd1;
                2'b01: occupancy <= occupancy - 2'd1;
                default: begin end
            endcase
            if (push) begin
                if (write_slot)
                    queue1 <= decoded;
                else
                    queue0 <= decoded;
                write_slot <= !write_slot;
            end
            if (pop)
                read_slot <= !read_slot;
        end
    end
endmodule
