`timescale 1ns/1ps
`default_nettype none

// Login digit conversion is serialized independently of the data-plane parser.
// Binary-to-ASCII uses shift/add-3; the inverse uses a checked base-10 fold.
module ouch_decimal64 (
    input wire clk,
    input wire flush,
    input wire s_valid,
    output wire s_ready,
    input wire s_decode,
    input wire [63:0] s_number,
    input wire [159:0] s_text,
    output wire m_valid,
    input wire m_ready,
    output logic m_ok,
    output logic [63:0] m_number,
    output logic [159:0] m_text
);
    localparam logic [1:0] IDLE = 0, ENCODE = 1, DECODE = 2, RESULT = 3;
    logic [1:0] state;
    logic [5:0] cycle;
    logic [63:0] shift_number;
    logic [79:0] bcd;
    logic [79:0] corrected;
    logic [79:0] next_bcd;
    logic [159:0] shift_text;
    logic [63:0] result_number;
    logic [67:0] candidate;
    logic bad_digit;
    logic seen_digit;
    logic decode_bad;
    wire [7:0] character = shift_text[159:152];
    wire is_digit = character >= "0" && character <= "9";
    wire [3:0] digit = character[3:0];

    assign s_ready = !flush && state == IDLE;
    assign m_valid = !flush && state == RESULT;

    // Convert packed digits into ASCII and fill unused leading columns with spaces.
    function automatic [159:0] format_bcd(input logic [79:0] packed_digits);
        logic leading;
        logic [3:0] d;
        integer i;
        begin
            leading = 1'b1;
            for (i = 19; i >= 0; i = i - 1) begin
                d = packed_digits[i*4 +: 4];
                if (leading && d == 0 && i != 0) begin
                    format_bcd[i*8 +: 8] = " ";
                end else begin
                    leading = 1'b0;
                    format_bcd[i*8 +: 8] = {4'h3, d};
                end
            end
        end
    endfunction

    always @* begin
        corrected = bcd;
        for (integer i = 0; i < 20; i = i + 1)
            if (bcd[i*4 +: 4] >= 5)
                corrected[i*4 +: 4] = bcd[i*4 +: 4] + 4'd3;
        next_bcd = {corrected[78:0], shift_number[63]};
        candidate = ({4'b0, result_number} << 3) +
                    ({4'b0, result_number} << 1) + 68'(digit);
        bad_digit = !is_digit && (character != " " || seen_digit);
        decode_bad = bad_digit || (is_digit && |candidate[67:64]);
    end

    always @(posedge clk) begin
        if (flush) begin
            state <= IDLE;
        end else begin
            case (state)
                IDLE: if (s_valid && s_ready) begin
                    state <= s_decode ? DECODE : ENCODE;
                    cycle <= 0;
                    shift_number <= s_number;
                    bcd <= 0;
                    shift_text <= s_text;
                    result_number <= 0;
                    seen_digit <= 1'b0;
                    m_ok <= 1'b1;
                    m_number <= 0;
                    m_text <= 0;
                end
                ENCODE: begin
                    shift_number <= shift_number << 1;
                    bcd <= next_bcd;
                    cycle <= cycle + 1'b1;
                    if (cycle == 63) begin
                        m_text <= format_bcd(next_bcd);
                        state <= RESULT;
                    end
                end
                DECODE: begin
                    shift_text <= shift_text << 8;
                    cycle <= cycle + 1'b1;
                    if (is_digit) begin
                        result_number <= candidate[63:0];
                        seen_digit <= 1'b1;
                    end
                    if (decode_bad) m_ok <= 1'b0;
                    if (cycle == 19) begin
                        m_number <= is_digit ? candidate[63:0] : result_number;
                        m_ok <= m_ok && !decode_bad && (seen_digit || is_digit);
                        state <= RESULT;
                    end
                end
                RESULT: if (m_valid && m_ready) state <= IDLE;
                default: state <= IDLE;
            endcase
        end
    end
endmodule

`default_nettype wire
