`timescale 1ns/1ps

module tb_histogram_swap;
    localparam integer BINS = 8;
    localparam integer CLASSES = 2;
    localparam integer WORDS = 1 + CLASSES*(2+BINS);
    logic clk = 0;
    always #5 clk = !clk;
    logic reset = 1;
    logic sample_valid = 0, sample_class = 0;
    logic [63:0] sample_latency = 0;
    logic snapshot_valid = 0;
    wire snapshot_ready;
    logic [63:0] snapshot_tick = 0;
    wire m_valid, m_first, m_last;
    logic m_ready = 0;
    wire [127:0] m_word;
    wire [31:0] completed;
    wire busy;
    latency_histogram #(.CLASSES(CLASSES), .BINS(BINS), .COUNTER_W(12)) dut (.*);

    integer seen = 0;
    integer observed_frame = 0;
    integer observed_count [0:CLASSES-1];
    integer observed_sum [0:CLASSES-1];
    integer bucket_total [0:CLASSES-1];
    integer expected_samples [0:1][0:CLASSES-1];
    integer expected_ticks [0:1][0:CLASSES-1];

    always @(posedge clk) begin
        integer local_word, event_class;
        if (m_valid && m_ready) begin
            if (m_first) begin
                if (m_word[127:64] != {32'h4c545331,32'(observed_frame)})
                    $fatal(1,"Snapshot header missed the expected frame");
                seen = 1;
                for (integer i = 0; i < CLASSES; i++) begin
                    observed_count[i] = 0;
                    observed_sum[i] = 0;
                    bucket_total[i] = 0;
                end
            end else begin
                local_word = (seen-1) % (BINS+2);
                event_class = (seen-1) / (BINS+2);
                if (event_class >= CLASSES || m_word[123:116] !== 8'(event_class))
                    $fatal(1,"Unexpected class in concurrent snapshot");
                case (local_word)
                    0: begin
                        if (m_word[127:124] != 1 || m_word[99])
                            $fatal(1,"Expected nonoverflowed class totals");
                        observed_count[event_class] = int'(m_word[95:64]);
                        observed_sum[event_class] = int'(m_word[63:0]);
                    end
                    1: begin
                        if (m_word[127:124] != 2) $fatal(1,"Bounds record missing");
                    end
                    default: begin
                        if (m_word[127:124] != 3 || m_word[115:108] != 8'(local_word-2))
                            $fatal(1,"Bin sequence mismatch");
                        bucket_total[event_class] += int'(m_word[31:0]);
                    end
                endcase
                if (m_last) begin
                    if (seen != WORDS - 1) $fatal(1,"Concurrent frame truncated");
                    for (integer i = 0; i < CLASSES; i++) begin
                        if (observed_count[i] != expected_samples[observed_frame][i] ||
                            observed_sum[i] != expected_ticks[observed_frame][i] ||
                            bucket_total[i] != observed_count[i])
                            $fatal(1,"Frame %0d class %0d crosses sample bank",observed_frame,i);
                    end
                    observed_frame++;
                end
                seen++;
            end
        end
    end

    task automatic sample(input logic group, input integer duration);
        begin
            sample_valid = 1;
            sample_class = group;
            sample_latency = 64'(duration);
            @(negedge clk);
            sample_valid = 0;
        end
    endtask

    initial begin
        expected_samples[0][0] = 1;
        expected_samples[0][1] = 1;
        expected_ticks[0][0] = 5;
        expected_ticks[0][1] = 3;
        expected_samples[1][0] = 22;
        expected_samples[1][1] = 2;
        expected_ticks[1][0] = 46;
        expected_ticks[1][1] = 153;
        repeat (4) @(negedge clk);
        reset = 0;
        sample(0,5);
        snapshot_tick = 100;
        snapshot_valid = 1;
        sample(1,3);
        snapshot_valid = 0;
        for (integer i = 0; i < 20; i++) sample(0,2);
        if (!m_valid || !m_first || !busy || snapshot_ready)
            $fatal(1,"Histogram did not maintain a closed bank");
        sample(1,75);
        m_ready = 1;
        wait (observed_frame == 1);
        @(negedge clk);
        if (snapshot_ready) $fatal(1,"Scrub not performed");
        sample(0,4);
        sample(1,78);
        while (!snapshot_ready) @(negedge clk);
        snapshot_tick = 200;
        snapshot_valid = 1;
        sample(0,2);
        snapshot_valid = 0;
        wait (observed_frame == 2);
        repeat (2) @(negedge clk);
        if (completed != 2) $fatal(1,"Frame tally wrong");
        $display("PASS histogram bank swap: same-edge closing sample, continued input during egress and scrub");
        $finish;
    end
    initial begin #50000; $fatal(1,"Bank concurrency timeout"); end
endmodule
