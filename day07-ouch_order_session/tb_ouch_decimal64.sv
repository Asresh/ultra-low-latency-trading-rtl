`timescale 1ns/1ps
`default_nettype none

module tb_ouch_decimal64;
    logic clk = 0;
    always #5 clk = ~clk;
    logic flush, s_valid, s_decode;
    wire s_ready;
    logic [63:0] s_number;
    logic [159:0] s_text;
    wire m_valid, m_ok;
    logic m_ready;
    wire [63:0] m_number;
    wire [159:0] m_text;

    ouch_decimal64 dut (.*);

    bit previous_stall = 0;
    logic [224:0] previous_result;
    integer inputs = 0, outputs = 0, timeouts = 0;
    integer stalls = 0;
    always @(posedge clk) begin
        if (flush) begin
            if (s_ready || m_valid) $fatal(1, "decimal result survived transport flush");
            previous_stall = 0;
        end else begin
            if (previous_stall && (!m_valid || previous_result !== {m_ok, m_number, m_text}))
                $fatal(1, "decimal conversion changed a held result");
            if (s_valid && s_ready) inputs = inputs + 1;
            if (m_valid && m_ready) outputs = outputs + 1;
            if (m_valid && !m_ready) stalls = stalls + 1;
            previous_stall = m_valid && !m_ready;
            previous_result = {m_ok, m_number, m_text};
        end
    end

    task automatic start(
        input logic decode,
        input logic [63:0] number,
        input logic [159:0] text
    );
        begin
            @(negedge clk);
            if (!s_ready) $fatal(1, "idle converter refused work");
            s_valid = 1;
            s_decode = decode;
            s_number = number;
            s_text = text;
            @(posedge clk);
            @(negedge clk);
            s_valid = 0;
        end
    endtask

    task automatic convert(
        input logic decode,
        input logic [63:0] raw_number,
        input logic [159:0] raw_text,
        input logic expected_ok,
        input logic [63:0] expected_number,
        input logic [159:0] expected_text,
        input integer hold_cycles
    );
        integer duration;
        begin
            start(decode, raw_number, raw_text);
            duration = decode ? 20 : 64;
            for (integer i = 0; i < duration; i = i + 1) begin
                if (m_valid || s_ready)
                    $fatal(1, "decode=%b responded early or exposed a second slot at cycle %0d", decode, i);
                @(negedge clk);
            end
            if (!m_valid || (m_ok !== expected_ok) || s_ready)
                $fatal(1, "conversion result/valid missing at known deadline");
            if (expected_ok && (m_number !== expected_number || m_text !== expected_text))
                $fatal(1, "decimal mismatch for input %h '%s': expected %h '%s' got %h '%s'",
                       raw_number, raw_text, expected_number, expected_text, m_number, m_text);
            repeat (hold_cycles) @(negedge clk);
            m_ready = 1;
            @(posedge clk);
            @(negedge clk);
            m_ready = 0;
            if (!s_ready || m_valid) $fatal(1, "converter did not release consumed result");
        end
    endtask

    function automatic [159:0] print_value(input logic [63:0] number);
        string text;
        begin
            text = $sformatf("%20d", number);
            for (integer i = 0; i < 20; i = i + 1)
                print_value[(19-i)*8 +: 8] = text[i];
        end
    endfunction

    task automatic round_trip(input logic [63:0] number, input integer holds);
        logic [159:0] canonical;
        begin
            canonical = print_value(number);
            convert(0, number, 0, 1, 0, canonical, holds);
            convert(1, 0, canonical, 1, number, 0, holds >> 1);
        end
    endtask

    task automatic interrupt(input logic decode, input integer before_cycle);
        begin
            start(decode, 64'hffff_ffff_ffff_ffff, "18446744073709551615");
            repeat (before_cycle) @(negedge clk);
            flush = 1;
            repeat (2) @(negedge clk);
            flush = 0;
            repeat (2) @(negedge clk);
            if (!s_ready || m_valid)
                $fatal(1, "partial conversion survived across a new socket");
            timeouts = timeouts + 1;
        end
    endtask

    function automatic [63:0] scramble(input logic [63:0] number);
        logic [63:0] x;
        begin
            x = number ^ (number << 13);
            x = x ^ (x >> 7);
            scramble = x ^ (x << 17);
        end
    endfunction

    logic [63:0] seed = 64'heea2_b806_326a_1039;
    logic [63:0] decade;
    initial begin
        flush = 1;
        s_valid = 0;
        s_decode = 0;
        m_ready = 0;
        s_number = 0;
        s_text = 0;
        repeat (3) @(negedge clk);
        flush = 0;

        round_trip(0, 4);
        round_trip(1, 4);
        round_trip(64'hffff_ffff_ffff_ffff, 3);
        round_trip(64'hffff_ffff_ffff_fffe, 3);
        round_trip(64'h8000_0000_0000_0000, 3);
        round_trip(64'h7fff_ffff_ffff_ffff, 3);
        decade = 1;
        for (integer i = 1; i <= 19; i = i + 1) begin
            decade = decade * 10;
            round_trip(decade - 1, 2);
            round_trip(decade, 1);
            round_trip(decade + 1, 3);
        end
        convert(1, 0, "00000000000000000001", 1, 1, 0, 3);
        convert(1, 0, "18446744073709551616", 0, 0, 0, 2);
        convert(1, 0, "99999999999999999999", 0, 0, 0, 0);
        convert(1, 0, "                    ", 0, 0, 0, 0);
        convert(1, 0, "                 1 2", 0, 0, 0, 1);
        convert(1, 0, "                  0 ", 0, 0, 0, 1);
        convert(1, 0, "                  -1", 0, 0, 0, 1);
        convert(1, 0, "                  +1", 0, 0, 0, 1);
        convert(1, 0, "                 A14", 0, 0, 0, 1);
        for (integer i = 0; i < 256; i = i + 1) begin
            seed = scramble(seed);
            round_trip(seed, int'(seed[2:0]));
        end
        interrupt(0, 0);
        interrupt(0, 35);
        interrupt(0, 64);
        interrupt(1, 0);
        interrupt(1, 10);
        interrupt(1, 20);
        round_trip(231, 3);
        if (inputs != outputs + timeouts || stalls == 0)
            $fatal(1, "conversion result accounting diverged");
        $display("PASS decimal64: %0d conversions, %0d exact socket flushes under %0d held cycles",
                 outputs, timeouts, stalls);
        $finish;
    end

    initial begin
        #2000000;
        $fatal(1, "decimal converter watchdog");
    end
endmodule

`default_nettype wire
