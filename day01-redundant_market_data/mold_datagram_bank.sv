`timescale 1ns/1ps

// A whole packet must pass its count/length checks before any block can leave.
module mold_datagram_bank #(
    parameter integer PACKET_BYTES = 1536,
    parameter integer MESSAGE_BYTES = 64,
    parameter integer BLOCKS = 64
) (
    input  logic                        clk,
    input  logic                        clear,
    input  logic                        session_active,
    input  logic [79:0]                 session_id,

    input  logic                        rx_valid,
    output logic                        rx_ready,
    input  logic [63:0]                 rx_data,
    input  logic [7:0]                  rx_keep,
    input  logic                        rx_first,
    input  logic                        rx_last,

    output logic                        candidate_valid,
    input  logic                        candidate_ready,
    output logic [2:0]                  candidate_kind,
    output logic [63:0]                 candidate_seq,
    output logic [15:0]                 candidate_length,
    output logic [MESSAGE_BYTES*8-1:0] candidate_payload
);
    localparam logic [2:0] DATA = 3'd0, HEARTBEAT = 3'd1, END_SESSION = 3'd2,
                           BAD_PACKET = 3'd3, BOUND = 3'd4, OTHER_SESSION = 3'd5;
    localparam integer ADDR_BITS = $clog2((PACKET_BYTES+7)/8);
    localparam integer POS_BITS = $clog2(PACKET_BYTES+9);
    localparam integer BLOCK_BITS = $clog2(BLOCKS);
    localparam integer WORDS_PER_MESSAGE = (MESSAGE_BYTES+7)/8;
    localparam integer MESSAGE_WORD_BITS = $clog2(WORDS_PER_MESSAGE);
    localparam logic [POS_BITS:0] MAX_LENGTH = (POS_BITS+1)'(PACKET_BYTES);
    localparam logic [15:0] MAX_BLOCKS = 16'(BLOCKS);
    localparam logic [15:0] MAX_BODY = 16'(MESSAGE_BYTES);

    typedef enum logic [3:0] {
        CAPTURE, HEADER, READ_LENGTH, CHECK_LENGTH, READ_DESCRIPTOR, LOAD_DESCRIPTOR,
        READ_PAYLOAD, LOAD_PAYLOAD, EMIT, NOTICE
    } state_t;

    state_t state;
    logic [POS_BITS-1:0] capture_offset, packet_size;
    logic capture_fault, capture_limit;
    logic [79:0] packet_session;
    logic [63:0] packet_sequence, block_sequence;
    logic [15:0] packet_count;
    logic [2:0] packet_notice;

    logic [POS_BITS-1:0] cursor, payload_position;
    logic [BLOCK_BITS-1:0] block_index;
    logic [POS_BITS+15:0] descriptors [0:BLOCKS-1];
    logic [POS_BITS+15:0] descriptor;
    logic [15:0] payload_length, remaining;
    logic [63:0] payload_words [0:WORDS_PER_MESSAGE-1];
    logic [MESSAGE_WORD_BITS-1:0] payload_word;

    logic accepted, ram_write, ram_read, framing_fault, descriptor_write;
    logic [POS_BITS:0] capture_end;
    logic [ADDR_BITS-1:0] ram_read_word;
    logic [POS_BITS-1:0] next_payload_position, request_position;
    logic [127:0] ram_read_pair;
    logic [2:0] ram_shift;
    logic [63:0] aligned_bytes, valid_payload_bytes, net_word;
    logic [15:0] block_length, block_ordinal;
    logic [POS_BITS-1:0] block_start;
    logic [16:0] block_end;
    logic last_block, bad_block_shape;

    // Return the valid byte count for a contiguous low-byte enable mask.
    function automatic logic [3:0] keep_bytes(input logic [7:0] keep);
        case (keep)
            8'h01: keep_bytes = 4'd1;
            8'h03: keep_bytes = 4'd2;
            8'h07: keep_bytes = 4'd3;
            8'h0f: keep_bytes = 4'd4;
            8'h1f: keep_bytes = 4'd5;
            8'h3f: keep_bytes = 4'd6;
            8'h7f: keep_bytes = 4'd7;
            8'hff: keep_bytes = 4'd8;
            default: keep_bytes = 4'd0;
        endcase
    endfunction

    assign rx_ready = !clear && state == CAPTURE;
    assign accepted = rx_valid && rx_ready;
    assign capture_end = {1'b0, capture_offset} + (POS_BITS+1)'(keep_bytes(rx_keep));
    assign framing_fault = (rx_first != (capture_offset == 0))
                         || (rx_last ? keep_bytes(rx_keep) == 0 : rx_keep != 8'hff);
    assign ram_write = accepted && !capture_limit && capture_end <= MAX_LENGTH;

    assign next_payload_position = payload_position + POS_BITS'(8);
    assign ram_read = (state == READ_LENGTH) || (state == READ_PAYLOAD)
                   || (state == LOAD_PAYLOAD && remaining > 16'd8);
    assign request_position = state == READ_LENGTH ? cursor
                            : state == LOAD_PAYLOAD ? next_payload_position : payload_position;
    assign ram_read_word = ADDR_BITS'(request_position >> 3);
    assign aligned_bytes = 64'(ram_read_pair >> {ram_shift, 3'b000});

    mold_pair_ram #(.ADDR_BITS(ADDR_BITS)) packet_ram (
        .clk,
        .write_enable(ram_write), .write_word(capture_offset[ADDR_BITS+2:3]),
        .write_data(rx_data),
        .read_enable(ram_read), .read_word(ram_read_word), .read_pair(ram_read_pair)
    );

    for (genvar lane = 0; lane < 8; lane = lane + 1) begin : g_lanes
        assign net_word[63-lane*8 -: 8] = rx_data[lane*8 +: 8];
        assign valid_payload_bytes[lane*8 +: 8] =
            remaining > 16'(lane) ? aligned_bytes[lane*8 +: 8] : 8'b0;
    end

    assign block_length = {aligned_bytes[7:0], aligned_bytes[15:8]};
    assign block_start = cursor + POS_BITS'(2);
    assign block_end = 17'(cursor) + 17'd2 + {1'b0, block_length};
    assign block_ordinal = 16'(block_index) + 16'd1;
    assign last_block = block_ordinal == packet_count;
    assign bad_block_shape = (block_end > 17'(packet_size))
                          || (last_block && block_end != 17'(packet_size));
    assign descriptor_write = state == CHECK_LENGTH && !bad_block_shape
                           && block_length <= MAX_BODY;

    assign candidate_valid = !clear && ((state == EMIT) || (state == NOTICE));
    assign candidate_kind = state == EMIT ? DATA : packet_notice;
    assign candidate_seq = state == EMIT ? block_sequence : packet_sequence;
    assign candidate_length = state == EMIT ? payload_length : 16'd0;
    for (genvar word_index = 0; word_index < WORDS_PER_MESSAGE; word_index = word_index + 1)
        begin : g_message
        localparam integer OFFSET = 64 * word_index;
        localparam integer WIDTH = (MESSAGE_BYTES*8 - OFFSET >= 64)
                                 ? 64 : MESSAGE_BYTES*8 - OFFSET;
        assign candidate_payload[OFFSET +: WIDTH] = payload_words[word_index][0 +: WIDTH];
    end

    always_ff @(posedge clk) begin
        if (descriptor_write)
            descriptors[block_index] <= {block_start, block_length};
        if (state == READ_DESCRIPTOR)
            descriptor <= descriptors[block_index];
    end

    // Zero any bytes beyond a block's declared length, including entirely
    // unused words, so comparisons between feed copies are deterministic.
    always_ff @(posedge clk) begin
        if (state == LOAD_DESCRIPTOR)
            for (integer i = 0; i < WORDS_PER_MESSAGE; i = i + 1)
                payload_words[i] <= 64'd0;
        else if (state == LOAD_PAYLOAD)
            payload_words[payload_word] <= valid_payload_bytes;
        if (ram_read)
            ram_shift <= request_position[2:0];
    end

    always_ff @(posedge clk) begin
        if (clear) begin
            state            <= CAPTURE;
            capture_offset   <= '0;
            capture_fault    <= 1'b0;
            capture_limit    <= 1'b0;
            packet_size      <= '0;
            packet_session   <= '0;
            packet_sequence  <= '0;
            packet_count     <= '0;
            packet_notice    <= '0;
            cursor           <= '0;
            block_index      <= '0;
            block_sequence   <= '0;
            payload_position <= '0;
            payload_length   <= '0;
            remaining        <= '0;
            payload_word     <= '0;
        end else begin
            case (state)
                CAPTURE: if (accepted) begin
                    capture_fault <= capture_fault || framing_fault;
                    capture_limit <= capture_limit || capture_end > MAX_LENGTH;
                    if (!capture_limit && capture_end <= MAX_LENGTH) begin
                        capture_offset <= rx_last ? capture_end[POS_BITS-1:0]
                                                   : capture_offset + POS_BITS'(8);
                        case (capture_offset)
                            POS_BITS'(0): packet_session[79:16] <= net_word;
                            POS_BITS'(8): begin
                                packet_session[15:0]  <= net_word[63:48];
                                packet_sequence[63:16] <= net_word[47:0];
                            end
                            POS_BITS'(16): begin
                                packet_sequence[15:0] <= net_word[63:48];
                                packet_count         <= net_word[47:32];
                            end
                            default: begin end
                        endcase
                    end
                    if (rx_last) begin
                        packet_size <= capture_end[POS_BITS-1:0];
                        state       <= HEADER;
                    end
                end
                HEADER: begin
                    // Most header rejection cases share the NOTICE path.
                    state <= NOTICE;
                    if (capture_fault || packet_size < POS_BITS'(20))
                        packet_notice <= BAD_PACKET;
                    else if (capture_limit)
                        packet_notice <= BOUND;
                    else if (!session_active || packet_session != session_id)
                        packet_notice <= OTHER_SESSION;
                    else if (packet_count == 0 || packet_count == 16'hffff) begin
                        if (packet_size != POS_BITS'(20))
                            packet_notice <= BAD_PACKET;
                        else
                            packet_notice <= packet_count == 0 ? HEARTBEAT : END_SESSION;
                    end else if (packet_count > MAX_BLOCKS) begin
                        packet_notice <= BOUND;
                    end else begin
                        cursor         <= POS_BITS'(20);
                        block_index    <= '0;
                        block_sequence <= packet_sequence;
                        state          <= READ_LENGTH;
                    end
                end
                READ_LENGTH: begin
                    if (17'(cursor) + 17'd2 <= 17'(packet_size))
                        state <= CHECK_LENGTH;
                    else begin
                        packet_notice <= BAD_PACKET;
                        state         <= NOTICE;
                    end
                end
                CHECK_LENGTH: begin
                    if (bad_block_shape) begin
                        packet_notice <= BAD_PACKET;
                        state         <= NOTICE;
                    end else if (block_length > MAX_BODY) begin
                        packet_notice <= BOUND;
                        state         <= NOTICE;
                    end else if (last_block) begin
                        block_index <= '0;
                        state       <= READ_DESCRIPTOR;
                    end else begin
                        cursor      <= block_end[POS_BITS-1:0];
                        block_index <= block_index + 1'b1;
                        state       <= READ_LENGTH;
                    end
                end
                READ_DESCRIPTOR: state <= LOAD_DESCRIPTOR;
                LOAD_DESCRIPTOR: begin
                    {payload_position, payload_length} <= descriptor;
                    remaining    <= descriptor[15:0];
                    payload_word <= '0;
                    if (descriptor[15:0] == 0)
                        state <= EMIT;
                    else
                        state <= READ_PAYLOAD;
                end
                READ_PAYLOAD: state <= LOAD_PAYLOAD;
                LOAD_PAYLOAD: begin
                    if (remaining <= 16'd8) begin
                        state <= EMIT;
                    end else begin
                        remaining        <= remaining - 16'd8;
                        payload_position <= next_payload_position;
                        payload_word     <= payload_word + 1'b1;
                    end
                end
                EMIT: if (candidate_ready) begin
                    if (last_block) begin
                        state           <= CAPTURE;
                        capture_offset  <= '0;
                        capture_fault   <= 1'b0;
                        capture_limit   <= 1'b0;
                        packet_sequence <= '0;
                    end else begin
                        block_sequence <= block_sequence + 64'd1;
                        block_index    <= block_index + 1'b1;
                        state          <= READ_DESCRIPTOR;
                    end
                end
                NOTICE: if (candidate_ready) begin
                    state           <= CAPTURE;
                    capture_offset  <= '0;
                    capture_fault   <= 1'b0;
                    capture_limit   <= 1'b0;
                    packet_sequence <= '0;
                end
                default: state <= CAPTURE;
            endcase
        end
    end
endmodule
