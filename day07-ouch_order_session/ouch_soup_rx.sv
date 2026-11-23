`timescale 1ns/1ps
`default_nettype none

module ouch_soup_rx (
    input wire clk,
    input wire flush,
    input wire s_valid,
    output wire s_ready,
    input wire [63:0] s_data,
    input wire [7:0] s_keep,
    output wire m_valid,
    input wire m_ready,
    output ouch_wire_pkg::packet_t m_packet,
    output ouch_wire_pkg::status_t fault
);
    import ouch_wire_pkg::*;

    localparam logic [1:0] HEADER = 0, BODY = 1, OUTPUT = 2;
    logic [1:0] phase;
    logic [127:0] reservoir;
    logic [4:0] stored;
    logic [3:0] in_length;
    logic [3:0] take;
    frame_length_t written;
    frame_length_t remaining;
    body_t packet_body;
    frame_length_t packet_length;
    status_t fault_q;
    logic bad_keep_pending;
    logic [127:0] append_bytes;
    logic [127:0] retained;
    logic [4:0] after_take;
    logic [63:0] body_chunk;
    body_t aligned_chunk;
    wire [15:0] length_word = {reservoir[7:0], reservoir[15:8]};
    wire in_fire = s_valid && s_ready;
    wire good_keep = s_keep != 0 && ((s_keep & (s_keep + 1'b1)) == 0);

    assign s_ready = !flush && fault_q == ST_OK && !bad_keep_pending && stored <= 8;
    assign m_valid = !flush && phase == OUTPUT;
    assign m_packet = {packet_length, packet_body};
    assign fault = fault_q;

    always @* begin
        in_length = 0;
        append_bytes = '0;
        for (integer i = 0; i < 8; i = i + 1) begin
            if (s_keep[i]) begin
                in_length = in_length + 1'b1;
                append_bytes[i*8 +: 8] = s_data[i*8 +: 8];
            end
        end
    end

    always @* begin
        take = 0;
        if (fault_q == ST_OK) begin
            if (phase == HEADER && stored >= 2) begin
                take = 2;
            end else if (phase == BODY) begin
                if (stored < 8) take = stored[3:0];
                else take = 8;
                if (remaining < frame_length_t'(take)) take = 4'(remaining);
            end
        end
        after_take = stored - {1'b0, take};
        retained = reservoir >> (take * 8);
        body_chunk = '0;
        for (integer j = 0; j < 8; j = j + 1)
            if (j < int'(take)) body_chunk[j*8 +: 8] = reservoir[j*8 +: 8];
        aligned_chunk = body_t'(body_chunk) << (written * 8);
    end

    always @(posedge clk) begin
        if (flush) begin
            reservoir <= '0;
            stored <= '0;
            written <= '0;
            remaining <= '0;
            phase <= HEADER;
            fault_q <= ST_OK;
            bad_keep_pending <= 1'b0;
        end else if (fault_q == ST_OK) begin
            // A bad beat is after every byte already in the reservoir.
            // Preserve complete preceding frames while fencing new input.
            if (in_fire && !good_keep)
                bad_keep_pending <= 1'b1;
            if (in_fire && good_keep) begin
                reservoir <= retained | (append_bytes << (after_take * 8));
                stored <= after_take + {1'b0, in_length};
            end else if (take != 0) begin
                reservoir <= retained;
                stored <= after_take;
            end

            if (bad_keep_pending && phase != OUTPUT && take == 0)
                fault_q <= ST_BAD_KEEP;

            case (phase)
                HEADER: if (take != 0) begin
                    if (length_word == 0 || length_word > 16'(MAX_BODY)) begin
                        fault_q <= ST_FRAME_LENGTH;
                    end else begin
                        packet_body <= '0;
                        packet_length <= frame_length_t'(length_word);
                        written <= '0;
                        remaining <= frame_length_t'(length_word);
                        phase <= BODY;
                    end
                end
                BODY: if (take != 0) begin
                    packet_body <= packet_body | aligned_chunk;
                    written <= written + frame_length_t'(take);
                    remaining <= remaining - frame_length_t'(take);
                    if (frame_length_t'(take) == remaining) phase <= OUTPUT;
                end
                OUTPUT: if (m_valid && m_ready) phase <= HEADER;
                default: phase <= HEADER;
            endcase
        end
    end
endmodule

`default_nettype wire
