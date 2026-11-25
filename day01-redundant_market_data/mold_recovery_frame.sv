`timescale 1ns/1ps
`default_nettype none
`include "mold_recovery_codes.svh"

// Validate the complete on-wire packet before replaying its original words.
module mold_recovery_frame #(
    parameter integer PACKET_BYTES = 256,
    parameter integer MESSAGE_BYTES = 64,
    parameter integer BLOCKS = 16,
    parameter integer KEY_W = 2,
    parameter integer EPOCH_W = 16,
    parameter integer TOKEN_W = 16,
    localparam integer WORDS = (PACKET_BYTES + 7) / 8,
    localparam integer ADDR_W = WORDS > 4 ? $clog2(WORDS) : 2
) (
    input wire clk, rst,
    input wire rx_valid,
    output wire rx_ready,
    input wire [63:0] rx_data,
    input wire [7:0] rx_keep,
    input wire rx_first, rx_last,
    input wire [KEY_W-1:0] rx_slot,
    input wire [EPOCH_W-1:0] rx_epoch,
    input wire [TOKEN_W-1:0] rx_token,
    output wire descriptor_valid,
    input wire descriptor_ready, descriptor_pass,
    output logic [KEY_W-1:0] packet_slot,
    output logic [EPOCH_W-1:0] packet_epoch,
    output logic [TOKEN_W-1:0] packet_token,
    output logic [79:0] packet_session,
    output logic [63:0] packet_sequence,
    output logic [15:0] packet_count, packet_bytes,
    output logic [3:0] frame_status,
    output wire replay_valid,
    input wire replay_ready,
    output wire [63:0] replay_data,
    output wire [7:0] replay_keep,
    output wire replay_first, replay_last
);
    typedef enum logic [2:0] {CAPTURE, HEADER, LENGTH_READ, LENGTH_CHECK, DECIDE, REPLAY} state_t;
    state_t state;
    logic packet_open, framing_fault, oversize;
    logic [7:0] tail_keep;
    logic [ADDR_W-1:0] captured_word, last_word, replay_word;
    logic [15:0] cursor, block_ordinal;
    logic [2:0] read_shift;
    wire [127:0] read_pair;
    wire [15:0] aligned = 16'(read_pair >> {read_shift,3'b000});
    wire [15:0] length = {aligned[7:0],aligned[15:8]};
    wire [16:0] block_end = {1'b0,cursor} + 17'd2 + {1'b0,length};
    wire final_block = block_ordinal == packet_count;
    wire accepted = rx_valid && rx_ready;
    wire replay_start = descriptor_valid && descriptor_ready && descriptor_pass;
    wire replay_next = replay_valid && replay_ready && !replay_last;
    wire ram_read = state == LENGTH_READ || replay_start || replay_next;
    wire [ADDR_W-1:0] read_word = state == LENGTH_READ ? ADDR_W'(cursor >> 3) :
                                 replay_start ? '0 : replay_word + 1'b1;
    wire [3:0] keep_count = prefix_bytes(rx_keep);
    wire [16:0] capture_end = {1'b0,packet_bytes} + {13'd0,keep_count};
    wire store_word = accepted && !oversize && capture_end <= 17'(PACKET_BYTES)
                    && int'(captured_word) < WORDS;
    wire malformed_beat = rx_first == packet_open ||
                          (rx_last ? keep_count == 0 : rx_keep != 8'hff);
    wire [63:0] network_word;
    for (genvar lane = 0; lane < 8; lane++) begin : net
        assign network_word[63-8*lane-:8] = rx_keep[lane] ? rx_data[8*lane+:8] : 8'd0;
    end

    function automatic logic [3:0] prefix_bytes(input logic [7:0] keep);
        case (keep)
            8'h01: prefix_bytes = 1;
            8'h03: prefix_bytes = 2;
            8'h07: prefix_bytes = 3;
            8'h0f: prefix_bytes = 4;
            8'h1f: prefix_bytes = 5;
            8'h3f: prefix_bytes = 6;
            8'h7f: prefix_bytes = 7;
            8'hff: prefix_bytes = 8;
            default: prefix_bytes = 0;
        endcase
    endfunction

    mold_pair_ram #(.ADDR_BITS(ADDR_W)) packet_ram (
        .clk, .write_enable(store_word), .write_word(captured_word), .write_data(rx_data),
        .read_enable(ram_read), .read_word, .read_pair
    );

    assign rx_ready = !rst && state == CAPTURE;
    assign descriptor_valid = !rst && state == DECIDE;
    assign replay_valid = !rst && state == REPLAY;
    assign replay_last = replay_valid && replay_word == last_word;
    assign replay_first = replay_valid && replay_word == 0;
    assign replay_keep = replay_valid ? (replay_last ? tail_keep : 8'hff) : '0;

    // Mask ignored lanes even if the ingress adapter supplied stale bytes.
    for (genvar lane = 0; lane < 8; lane++) begin : output_lanes
        assign replay_data[8*lane+:8] = replay_keep[lane] ? read_pair[8*lane+:8] : '0;
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= CAPTURE;
            packet_open <= 0;
            framing_fault <= 0;
            oversize <= 0;
            tail_keep <= '0;
            captured_word <= '0;
            last_word <= '0;
            replay_word <= '0;
            packet_slot <= '0;
            packet_epoch <= '0;
            packet_token <= '0;
            packet_session <= '0;
            packet_sequence <= '0;
            packet_count <= '0;
            packet_bytes <= '0;
            frame_status <= `MRG_ACCEPT;
            cursor <= '0;
            block_ordinal <= '0;
            read_shift <= '0;
        end else begin
            case (state)
                CAPTURE: if (accepted) begin
                    packet_open <= !rx_last;
                    framing_fault <= framing_fault || malformed_beat;
                    oversize <= oversize || capture_end > 17'(PACKET_BYTES) ||
                                int'(captured_word) >= WORDS;
                    packet_bytes <= capture_end[16] ? '1 : capture_end[15:0];
                    if (!packet_open) begin
                        packet_slot <= rx_slot;
                        packet_epoch <= rx_epoch;
                        packet_token <= rx_token;
                        packet_session <= {network_word,16'd0};
                        packet_sequence <= '0;
                        packet_count <= '0;
                    end else if (captured_word == ADDR_W'(1)) begin
                        packet_session[15:0] <= network_word[63:48];
                        packet_sequence[63:16] <= network_word[47:0];
                    end else if (captured_word == ADDR_W'(2)) begin
                        packet_sequence[15:0] <= network_word[63:48];
                        packet_count <= network_word[47:32];
                    end
                    if (captured_word != '1)
                        captured_word <= captured_word + 1'b1;
                    if (rx_last) begin
                        tail_keep <= rx_keep;
                        last_word <= captured_word;
                        state <= HEADER;
                    end
                end
                HEADER: begin
                    state <= DECIDE;
                    if (framing_fault || packet_bytes < 16'd20)
                        frame_status <= `MRG_FRAME;
                    else if (oversize)
                        frame_status <= `MRG_LIMIT;
                    else if (packet_count == 0 || packet_count == 16'hffff)
                        frame_status <= packet_bytes == 16'd20 ? `MRG_EMPTY : `MRG_FRAME;
                    else if (packet_count > 16'(BLOCKS))
                        frame_status <= `MRG_LIMIT;
                    else begin
                        cursor <= 20;
                        block_ordinal <= 1;
                        state <= LENGTH_READ;
                    end
                end
                LENGTH_READ: begin
                    read_shift <= cursor[2:0];
                    state <= LENGTH_CHECK;
                end
                LENGTH_CHECK: begin
                    if ({1'b0,cursor} + 17'd2 > {1'b0,packet_bytes} ||
                        block_end > {1'b0,packet_bytes} ||
                        (final_block && block_end != {1'b0,packet_bytes})) begin
                        frame_status <= `MRG_FRAME;
                        state <= DECIDE;
                    end else if (length > 16'(MESSAGE_BYTES)) begin
                        frame_status <= `MRG_LIMIT;
                        state <= DECIDE;
                    end else if (final_block) begin
                        frame_status <= `MRG_ACCEPT;
                        state <= DECIDE;
                    end else begin
                        cursor <= block_end[15:0];
                        block_ordinal <= block_ordinal + 1'b1;
                        state <= LENGTH_READ;
                    end
                end
                DECIDE: if (descriptor_ready) begin
                    if (descriptor_pass) begin
                        replay_word <= '0;
                        state <= REPLAY;
                    end else begin
                        state <= CAPTURE;
                    end
                end
                REPLAY: if (replay_ready) begin
                    if (replay_last)
                        state <= CAPTURE;
                    else
                        replay_word <= replay_word + 1'b1;
                end
                default: state <= CAPTURE;
            endcase

            if ((state == DECIDE && descriptor_ready && !descriptor_pass) ||
                (state == REPLAY && replay_ready && replay_last)) begin
                framing_fault <= 0;
                oversize <= 0;
                captured_word <= '0;
                packet_bytes <= '0;
            end
        end
    end
endmodule

`default_nettype wire
