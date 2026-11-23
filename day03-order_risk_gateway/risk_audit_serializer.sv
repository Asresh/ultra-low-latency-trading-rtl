`timescale 1ns/1ps
`default_nettype none

// Convert one committed telemetry record into a little-endian 32-bit word stream.
// The wire CRC is the reflected IEEE CRC-32 used by zlib.
module risk_audit_serializer #(
    parameter integer PAYLOAD_BITS = 192,
    localparam integer PAYLOAD_WORDS = (PAYLOAD_BITS + 31) / 32,
    localparam integer STORED_BITS = 32 * PAYLOAD_WORDS,
    localparam integer FRAME_WORDS = PAYLOAD_WORDS + 4,
    localparam integer WORD_INDEX_W = $clog2(FRAME_WORDS)
) (
    input  wire                    clk,
    input  wire                    rst,
    input  wire                    s_valid,
    output wire                    s_ready,
    input  wire [PAYLOAD_BITS-1:0]  s_payload,
    output wire                    m_valid,
    input  wire                    m_ready,
    output reg  [31:0]             m_data,
    output wire                    m_first,
    output wire                    m_last
);
    localparam [31:0] MAGIC = 32'h314b_5352;
    localparam [31:0] DESCRIPTOR = {8'h01, 24'(PAYLOAD_BITS)};
    localparam [WORD_INDEX_W-1:0] FINAL_WORD = WORD_INDEX_W'(FRAME_WORDS - 1);

    reg active;
    reg [WORD_INDEX_W-1:0] word_index;
    reg [STORED_BITS-1:0] payload;
    reg [31:0] frame_id;
    reg [31:0] crc_accumulator;

    // Fold a word into the current CRC in the serializer's bit transmission order.
    function automatic [31:0] advance_crc(input [31:0] crc, input [31:0] word_data);
        reg [31:0] running;
        integer i;
        begin
            running = crc;
            for (i = 0; i < 32; i = i + 1)
                running = (running[0] ^ word_data[i]) ?
                    (running >> 1) ^ 32'hedb8_8320 : (running >> 1);
            advance_crc = running;
        end
    endfunction

    assign s_ready = !rst && !active;
    assign m_valid = !rst && active;
    assign m_first = m_valid && word_index == 0;
    assign m_last = m_valid && word_index == FINAL_WORD;

    always @* begin
        case (word_index)
            0: m_data = MAGIC;
            1: m_data = DESCRIPTOR;
            2: m_data = frame_id;
            FINAL_WORD: m_data = ~crc_accumulator;
            default: m_data = payload[31:0];
        endcase
    end

    always @(posedge clk) begin
        if (rst) begin
            active <= 1'b0;
            frame_id <= 0;
            word_index <= 0;
            crc_accumulator <= '1;
        end else if (s_valid && s_ready) begin
            active <= 1'b1;
            payload <= STORED_BITS'(s_payload);
            word_index <= 0;
            crc_accumulator <= '1;
        end else if (m_valid && m_ready) begin
            if (m_last) begin
                active <= 1'b0;
                frame_id <= frame_id + 1'b1;
            end else begin
                crc_accumulator <= advance_crc(crc_accumulator, m_data);
                word_index <= word_index + 1'b1;
                if (word_index >= 3)
                    payload <= payload >> 32;
            end
        end
    end
endmodule

`default_nettype wire
