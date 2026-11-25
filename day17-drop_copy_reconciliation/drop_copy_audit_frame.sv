`timescale 1ns/1ps
`default_nettype none

// Portable DCA1 transport. A frame consumes one input record and emits
// header, width, low-order payload words and a Castagnoli checksum.
module drop_copy_audit_frame #(
    parameter integer RECORD_W = 1005,
    localparam integer WORDS = (RECORD_W + 31) / 32,
    localparam integer WORD_W = $clog2(WORDS+3)
) (
    input wire clk, rst,
    input wire s_valid,
    output wire s_ready,
    input wire [RECORD_W-1:0] s_record,
    output wire m_valid,
    input wire m_ready,
    output logic [31:0] m_word,
    output wire m_first,
    output wire m_last
);
    localparam logic [31:0] MAGIC = 32'h3141_4344;
    localparam logic [31:0] FORMAT = {8'd1,24'(RECORD_W)};
    localparam logic [WORD_W-1:0] FOOTER = WORD_W'(WORDS+2);
    logic active;
    logic [WORD_W-1:0] position;
    logic [31:0] checksum;
    logic [32*WORDS-1:0] payload;
    assign s_ready = !rst && (!active || (m_last && m_ready));
    assign m_valid = !rst && active;
    assign m_first = m_valid && position == 0;
    assign m_last = m_valid && position == FOOTER;

    function automatic logic [31:0] crc(input logic [31:0] prior, word_value);
        logic [31:0] working;
        working = prior ^ word_value;
        for (integer bit_index = 0; bit_index < 32; bit_index++)
            working = (working >> 1) ^ (working[0] ? 32'h82f6_3b78 : 32'd0);
        return working;
    endfunction

    always @* begin
        m_word = '0;
        if (m_valid) begin
            if (position == 0)
                m_word = MAGIC;
            else if (position == 1)
                m_word = FORMAT;
            else if (position == FOOTER)
                m_word = ~checksum;
            else
                m_word = payload[(32'(position)-32'd2)*32 +: 32];
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            active <= 1'b0;
            position <= '0;
            checksum <= '1;
        end else if (s_valid && s_ready) begin
            active <= 1'b1;
            position <= '0;
            checksum <= '1;
            payload <= (32*WORDS)'(s_record);
        end else if (m_valid && m_ready) begin
            if (m_last)
                active <= 1'b0;
            else begin
                checksum <= crc(checksum,m_word);
                position <= position + 1'b1;
            end
        end
    end

`ifdef FORMAL
    logic initialized = 1'b0;
    always @(posedge clk) begin
        initialized <= 1'b1;
        if (initialized && !rst && !$past(rst)) begin
            if ($past(m_valid && !m_ready))
                assert(m_valid && $stable({m_word,m_first,m_last}));
            assert(!(m_first && m_last));
        end
    end
`endif
endmodule

`default_nettype wire
