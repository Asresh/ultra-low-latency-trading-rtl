`timescale 1ns/1ps

module tb_latency_trace;
    localparam integer DEPTH = 8;
    logic clk = 0;
    always #5 clk = !clk;
    logic reset = 1, arm = 0, enable = 1;
    logic [7:0] trigger_mask = 8'h10;
    logic [63:0] latency_threshold = 0;
    logic [2:0] post_count = 3;
    logic sample_valid = 0;
    logic [2:0] sample_status = 0;
    logic [63:0] sample_latency = 0, sample_tick = 0;
    logic [319:0] sample_data = 0;
    wire armed, frozen;
    wire [31:0] traces;
    wire m_valid, m_first, m_last;
    logic m_ready = 0;
    wire [319:0] m_data;
    latency_trace #(.ADDRESS_W(3), .DATA_W(320)) dut (.*);

    logic [319:0] expected [0:DEPTH];
    integer to_read, transferred = 0, stalled = 0;
    integer expected_frames = 0;
    logic held = 0;
    logic [321:0] pending;
    integer seen = 0;

    // Build the complete expected trace record for this fixture ID.
    function automatic logic [319:0] make_payload(input integer id);
        make_payload = 320'({32'(id),32'(id + 700),32'hd54f8129,
                             32'(id - 90),32'(id * 151),32'(id * 37)});
    endfunction

    // Pack the expected trace header, including sequence and trigger metadata.
    function automatic logic [319:0] make_header(
        input integer frame, input integer id, input integer count, input integer offset,
        input logic [2:0] cause, input logic high
    );
        make_header = {160'd0, 32'h4c545231, 32'(frame), 64'(id + 500), 8'(count),
                       8'(offset), cause, high, 12'd0};
    endfunction

    // Arm the recorder and wait until capture has begun.
    task automatic start_recording;
        begin
            @(negedge clk);
            arm = 1;
            @(negedge clk);
            arm = 0;
            if (!armed || frozen) $fatal(1, "Trace did not arm");
        end
    endtask

    // Submit one event and its timestamp to the trace recorder.
    task automatic insert(
        input integer index, input logic [2:0] status, input logic [63:0] delay
    );
        begin
            @(negedge clk);
            sample_valid = 1;
            sample_status = status;
            sample_latency = delay;
            sample_tick = 64'(index + 500);
            sample_data = make_payload(index);
            @(negedge clk);
            sample_valid = 0;
        end
    endtask

    // Populate the scoreboard with the expected trace header and records.
    task automatic set_expected(
        input integer frame, input integer first, input integer last, input integer trigger,
        input logic [2:0] status, input logic high
    );
        begin
            to_read = last-first+2;
            seen = 0;
            expected[0] = make_header(frame, trigger, last-first+1, trigger-first, status, high);
            for (integer k = first; k <= last; k++)
                expected[k-first+1] = make_payload(k);
        end
    endtask

    // Accept a complete report while verifying its frame boundaries.
    task automatic drain;
        begin
            if (!frozen || armed) $fatal(1, "Trace did not freeze");
            repeat (4) @(negedge clk);
            for (integer t = 0; t < 32 && seen < to_read; t++) begin
                m_ready = t % 4 != 0;
                @(negedge clk);
            end
            m_ready = 0;
            expected_frames++;
            if (frozen || armed || seen != to_read || traces != 32'(expected_frames))
                $fatal(1, "Trace drain complete state incorrect");
        end
    endtask

    always @(posedge clk) begin
        if (reset) begin
            held = 0;
        end else begin
            if (held && (!m_valid || {m_data,m_first,m_last} !== pending))
                $fatal(1, "Frozen trace changed while stalled");
            held = m_valid && !m_ready;
            if (held) begin
                pending = {m_data,m_first,m_last};
                stalled++;
            end
            if (m_valid && m_ready) begin
                if (seen >= to_read || m_data !== expected[seen] ||
                    m_first !== (seen == 0) || m_last !== (seen == to_read-1))
                    $fatal(1, "Trace %0d word %0d: %h != %h", expected_frames, seen,
                           m_data, expected[seen]);
                seen++;
                transferred++;
            end
        end
    end

    initial begin
        repeat (4) @(negedge clk);
        reset = 0;
        start_recording();
        for (integer i = 0; i < 18; i++)
            insert(i, 0, 64'(i));
        insert(18, 4, 0);
        insert(19, 0, 1);
        insert(20, 0, 1);
        if (frozen || !armed) $fatal(1, "Stopped before promised post-trigger observations");
        insert(21, 0, 1);
        set_expected(0, 14, 21, 18, 4, 0);
        // An arm request cannot overwrite a frame retained for a stalled host.
        arm = 1;
        insert(22, 4, 0);
        arm = 0;
        drain();

        @(negedge clk);
        post_count = 0;
        trigger_mask = 0;
        latency_threshold = 22;
        start_recording();
        insert(100, 0, 100);
        insert(101, 1, 21);
        if (frozen) $fatal(1, "Threshold fired below bound or on a non-match");
        insert(102, 1, 22);
        set_expected(1, 100, 102, 102, 1, 1);
        drain();

        start_recording();
        insert(436, 1, 11);
        enable = 0;
        @(negedge clk);
        if (armed || frozen || m_valid) $fatal(1, "Disabling trace did not abort capture");
        if (transferred != 13 || stalled <= 4) $fatal(1, "Incomplete trace coverage");
        $display("PASS trigger trace: two frames, %0d records and %0d held cycles", transferred, stalled);
        $finish;
    end
    initial begin #85000; $fatal(1, "Trace regression timeout"); end
endmodule
