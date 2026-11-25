`timescale 1ns/1ps
`default_nettype none

module tb_latency_period_divide;
    logic clk = 0, rst = 1, s_valid = 0, m_ready = 0;
    logic [63:0] s_reference_ns, s_ticks;
    wire s_ready, m_valid, m_overflow, m_zero_ticks;
    wire [63:0] m_period;
    always #5 clk = !clk;
    latency_period_divide dut (.*);

    integer file, fields, trials = 0, held = 0, overflowed = 0, zero = 0;
    logic [63:0] expected;
    logic expected_overflow, expected_zero;
    string stimulus;

    initial begin
        if (!$value$plusargs("vectors=%s",stimulus))
            $fatal(1,"configure +vectors=PATH");
        file = $fopen(stimulus,"r");
        if (!file)
            $fatal(1,"could not read %s",stimulus);
        repeat (3) @(negedge clk);
        rst = 0;
        while (!$feof(file)) begin
            fields = $fscanf(file,"%h %h %h %h %h\n",
                s_reference_ns,s_ticks,expected,expected_overflow,expected_zero);
            if (fields != 5)
                $fatal(1,"invalid divider fixture row %0d",trials);
            s_valid = 1;
            do @(posedge clk); while (!s_ready);
            @(negedge clk);
            s_valid = 0;
            if (!expected_zero) begin
                repeat (96) begin
                    if (m_valid || s_ready)
                        $fatal(1,"divider exposed a result during computation");
                    @(negedge clk);
                end
            end
            repeat (3 + trials % 9) begin
                if (!m_valid || s_ready ||
                    {m_overflow,m_zero_ticks,m_period} !== {expected_overflow,expected_zero,expected})
                    $fatal(1,"divider arithmetic or hold disagrees at case %0d",trials);
                held++;
                @(negedge clk);
            end
            m_ready = 1;
            @(negedge clk);
            m_ready = 0;
            trials++;
            overflowed += expected_overflow;
            zero += expected_zero;
        end
        $fclose(file);
        if (trials < 19 || overflowed < 2 || zero < 3)
            $fatal(1,"divider regression missed required cases");
        $display("PASS period divider: %0d exact ratios, %0d overflows, %0d zero divisors, %0d held cycles",
                 trials,overflowed,zero,held);
        $finish;
    end

    initial begin
        #300000;
        $fatal(1,"divider regression timed out");
    end
endmodule

`default_nettype wire
