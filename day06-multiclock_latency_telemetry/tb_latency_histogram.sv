`timescale 1ns/1ps

module tb_latency_histogram;
    localparam integer CLASSES = 4;
    localparam integer BINS = 12;
    logic clk = 0;
    always #5 clk = !clk;

    logic reset = 1;
    logic sample_valid = 0;
    logic [1:0] sample_class = 0;
    logic [63:0] sample_latency = 0;
    logic snapshot_valid = 0;
    wire snapshot_ready;
    logic [63:0] snapshot_tick = 0;
    wire m_valid, m_first, m_last;
    logic m_ready = 0;
    wire [127:0] m_word;
    wire [31:0] completed;
    wire busy;

    latency_histogram #(.CLASSES(CLASSES), .BINS(BINS), .COUNTER_W(6)) dut (.*);
    logic [31:0] rng = 32'hc005a8ef;
    integer blocks = 0;
    integer transferred = 0;
    logic was_blocked = 0;
    logic [129:0] held;
    integer frames;
    integer nwords, nsamples;
    integer words_read;
    integer rc;
    string filename;
    logic [127:0] expected [0:CLASSES*(BINS+2)];
    integer vectors;
    integer sample_total = 0;
    integer stalls = 0;

    function automatic logic [31:0] advance(input logic [31:0] value);
        logic [31:0] s;
        begin
            s = value ^ (value << 13);
            s = s ^ (s >> 17);
            advance = s ^ (s << 5);
        end
    endfunction

    always @(posedge clk) begin
        if (reset) begin
            was_blocked = 0;
        end else begin
            if (was_blocked && (!m_valid || {m_first,m_last,m_word} !== held))
                $fatal(1, "Snapshot word changed before transfer");
            was_blocked = m_valid && !m_ready;
            if (was_blocked) begin
                held = {m_first, m_last, m_word};
                stalls++;
            end
            if (m_valid && m_ready) begin
                if (m_word !== expected[words_read] ||
                    m_first !== (words_read == 0) ||
                    m_last !== (words_read == nwords-1))
                    $fatal(1, "Frame %0d word %0d: got %h expected %h", blocks, words_read,
                           m_word, expected[words_read]);
                words_read++;
                transferred++;
            end
        end
    end

    task automatic send_snapshot(
        input integer sample_count, input integer word_count, input logic [63:0] deadline
    );
        integer row, read_count;
        logic [1:0] group;
        logic [63:0] delay;
        begin
            if (sample_count < 0 || word_count != CLASSES*(BINS+2)+1)
                $fatal(1, "Bad histogram vector geometry");
            for (row = 0; row < sample_count; row++) begin
                read_count = $fscanf(vectors, "%d %h", group, delay);
                if (read_count != 2)
                    $fatal(1, "Missing sample at case %0d row %0d", blocks, row);
                @(negedge clk);
                sample_valid = 1;
                sample_class = group;
                sample_latency = delay;
            end
            @(negedge clk);
            sample_valid = 0;
            for (row = 0; row < word_count; row++) begin
                if ($fscanf(vectors, "%h", expected[row]) != 1)
                    $fatal(1, "Missing word %0d for case %0d", row, blocks);
            end
            words_read = 0;
            snapshot_tick = deadline;
            snapshot_valid = 1;
            do @(posedge clk); while (!snapshot_ready);
            @(negedge clk);
            snapshot_valid = 0;
            repeat (4) @(negedge clk);
            while (words_read < word_count) begin
                rng = advance(rng);
                m_ready = rng[3:0] > 4;
                @(negedge clk);
            end
            m_ready = 0;
            if (!busy || snapshot_ready) $fatal(1, "Snapshot bank was rearmed before scrub");
            do @(negedge clk); while (!snapshot_ready);
            if (busy || completed !== 32'(blocks+1))
                $fatal(1, "Frame %0d scrub did not free bank", blocks);
            sample_total += sample_count;
            blocks++;
        end
    endtask

    initial begin
        logic [63:0] cut_tick;
        if (!$value$plusargs("vectors=%s", filename)) $fatal(1, "Missing histogram vectors");
        vectors = $fopen(filename, "r");
        if (vectors == 0) $fatal(1, "Cannot open histogram vectors");
        frames = 0;
        words_read = 0;
        repeat (3) @(negedge clk);
        reset = 0;
        rc = $fscanf(vectors, "%d %d %h", nsamples, nwords, cut_tick);
        while (rc == 3) begin
            send_snapshot(nsamples, nwords, cut_tick);
            frames++;
            rc = $fscanf(vectors, "%d %d %h", nsamples, nwords, cut_tick);
        end
        if (!$feof(vectors) || frames != 4 || stalls == 0 || sample_total < 100)
            $fatal(1, "Incomplete histogram regression");
        $display("PASS histogram: %0d samples, %0d snapshots, %0d words and %0d held cycles",
                 sample_total, frames, transferred, stalls);
        $fclose(vectors);
        $finish;
    end
    initial begin #300000; $fatal(1, "Histogram timeout"); end
endmodule
