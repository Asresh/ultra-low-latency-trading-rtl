`timescale 1ns/1ps
module itch_decode_tb;
    localparam integer EVENT_BITS = 588;
    logic clk = 1'b0;
    always #5 clk = !clk;

    logic rst = 1'b1;
    logic s_valid = 1'b0;
    logic s_ready;
    logic [63:0] s_data = '0;
    logic [7:0] s_keep = '0;
    logic s_first = 1'b0, s_last = 1'b0;
    logic [63:0] s_sequence = '0;
    logic seed_valid = 1'b0, seed_ready;
    logic [63:0] seed_sequence = '0;
    logic m_valid, m_ready = 1'b0;
    wire [EVENT_BITS-1:0] event_record;

    itch_decode dut (
        .clk, .rst, .s_valid, .s_ready, .s_data, .s_keep, .s_first, .s_last, .s_sequence,
        .seed_valid, .seed_ready, .seed_sequence, .m_valid, .m_ready,
        .m_result(event_record[587:586]), .m_seq_status(event_record[585:584]),
        .m_sequence(event_record[583:520]), .m_expected_sequence(event_record[519:456]),
        .m_type(event_record[455:448]), .m_locate(event_record[447:432]),
        .m_tracking(event_record[431:416]), .m_timestamp(event_record[415:368]),
        .m_order_ref(event_record[367:304]), .m_new_order_ref(event_record[303:240]),
        .m_side(event_record[239:232]), .m_shares(event_record[231:200]),
        .m_stock(event_record[199:136]), .m_price(event_record[135:104]),
        .m_match(event_record[103:40]), .m_printable(event_record[39:32]),
        .m_mpid(event_record[31:0])
    );

    logic [EVENT_BITS-1:0] held_record, reference_record;
    integer cycle = 0;
    integer outputs = 0, inputs = 0;
    integer decoded_count = 0, unsupported_count = 0, malformed_count = 0;
    integer gap_count = 0, stale_count = 0;
    integer stalls = 0, blocked_ends = 0, full_midframe_beats = 0;
    integer stream_beats = 0, stream_results = 0, stream_latency_checks = 0;
    integer stream_start_cycle = 0, stream_end_cycle = 0;
    integer sink_mode = 0, stall_through = 0;
    integer rx, tx, stimulus_rc, expected_rc, opcode;
    logic [63:0] data_word, sequence_word;
    logic [7:0] keep_mask;
    logic first_value, last_value;
    logic held_valid = 1'b0, check_empty_latency = 1'b0;
    logic [31:0] sink_prng = 32'hfa80_e9c3, src_prng = 32'hc0fa_21ed;
    string directory;

    function automatic logic [31:0] next_random(input logic [31:0] prev);
        logic [31:0] x;
        begin
            x = prev ^ (prev << 13);
            x = x ^ (x >> 17);
            next_random = x ^ (x << 5);
        end
    endfunction

    task automatic drive_beat;
        begin
            @(negedge clk);
            src_prng = next_random(src_prng);
            if (sink_mode != 0 && src_prng[1:0] == 0) begin
                s_valid = 1'b0;
                @(negedge clk);
            end
            s_valid = 1'b1;
            s_data = data_word;
            s_keep = keep_mask;
            s_first = first_value;
            s_last = last_value;
            s_sequence = sequence_word;
            do @(posedge clk); while (!s_ready);
        end
    endtask

    task automatic drive_seed;
        begin
            @(negedge clk);
            s_valid = 1'b0;
            seed_sequence = data_word;
            seed_valid = 1'b1;
            do @(posedge clk); while (!seed_ready);
            @(negedge clk);
            seed_valid = 1'b0;
        end
    endtask

    task automatic drive_reset;
        begin
            @(negedge clk);
            s_valid = 1'b0;
            while (m_valid) @(negedge clk);
            rst = 1'b1;
            repeat (2) @(negedge clk);
            rst = 1'b0;
        end
    endtask

    always @(negedge clk) begin
        sink_prng = next_random(sink_prng);
        m_ready = sink_mode == 0 || (cycle >= stall_through && sink_prng[2:0] >= 3);
    end

    always @(posedge clk) begin
        cycle = cycle + 1;
        if (cycle > 100000)
            $fatal(1, "watchdog: parser or scoreboard deadlocked");

        if (rst) begin
            held_valid = 1'b0;
            check_empty_latency = 1'b0;
        end else begin
            if (held_valid && (!m_valid || event_record !== held_record))
                $fatal(1, "output changed during backpressure at cycle %0d", cycle);
            if (check_empty_latency) begin
                if (!m_valid)
                    $fatal(1, "expected next-edge availability after the accepted final beat");
                stream_latency_checks = stream_latency_checks + 1;
            end
            check_empty_latency = sink_mode == 0 && s_valid && s_ready && s_last
                                                      && !m_valid;
            held_valid = m_valid && !m_ready;
            if (held_valid) begin
                held_record = event_record;
                stalls = stalls + 1;
            end
            if (s_valid && !s_ready && s_last)
                blocked_ends = blocked_ends + 1;
            if (s_valid && s_ready) begin
                if (s_last)
                    inputs = inputs + 1;
                if (sink_mode == 0) begin
                    if (stream_beats == 0)
                        stream_start_cycle = cycle;
                    stream_end_cycle = cycle;
                    stream_beats = stream_beats + 1;
                end
                if (dut.occupancy == 2 && !s_last)
                    full_midframe_beats = full_midframe_beats + 1;
            end
            if (s_valid && sink_mode == 0 && !s_ready)
                $fatal(1, "unexpected input stall during unstalled line-rate block");
            if (m_valid && m_ready) begin
                expected_rc = $fscanf(rx, "%h", reference_record);
                if (expected_rc != 1)
                    $fatal(1, "extra completion %0d: %h", outputs, event_record);
                if (event_record !== reference_record)
                    $fatal(1, "completion %0d mismatch\n  got %h\n  exp %h",
                           outputs, event_record, reference_record);
                outputs = outputs + 1;
                if (sink_mode == 0)
                    stream_results = stream_results + 1;
                case (event_record[587:586])
                    2'd0: decoded_count = decoded_count + 1;
                    2'd1: unsupported_count = unsupported_count + 1;
                    2'd2: malformed_count = malformed_count + 1;
                    default: $fatal(1, "unknown completion result");
                endcase
                if (event_record[585:584] == 2'd2)
                    gap_count = gap_count + 1;
                if (event_record[585:584] == 2'd3)
                    stale_count = stale_count + 1;
            end
        end
    end

    initial begin
        if (!$value$plusargs("vectors=%s", directory))
            directory = "build";
        tx = $fopen({directory, "/stimulus.txt"}, "r");
        rx = $fopen({directory, "/expected.txt"}, "r");
        if (tx == 0 || rx == 0)
            $fatal(1, "could not open vector files in %s", directory);
        repeat (3) @(negedge clk);
        rst = 1'b0;

        while (!$feof(tx)) begin
            stimulus_rc = $fscanf(tx, "%d %h %h %d %d %h", opcode, data_word, keep_mask,
                          first_value, last_value, sequence_word);
            if (stimulus_rc == 6) begin
                case (opcode)
                    0: drive_beat();
                    1: drive_seed();
                    2: begin
                        // Let the last line-rate result leave before the long output stall.
                        @(negedge clk);
                        s_valid = 1'b0;
                        @(posedge clk);
                        @(negedge clk);
                        stall_through = cycle + 32;
                        sink_mode = 1;
                    end
                    3: drive_reset();
                    default: $fatal(1, "unknown stimulus opcode");
                endcase
            end else if (!$feof(tx)) begin
                $fatal(1, "stimulus file contains a malformed row");
            end
        end
        @(negedge clk);
        s_valid = 1'b0;
        repeat (40) @(negedge clk);

        if ($fscanf(rx, "%h", reference_record) == 1 || m_valid || inputs != outputs)
            $fatal(1, "missing completions: accepted %0d / retired %0d", inputs, outputs);
        if (stream_beats != 90 || stream_results != 21 || stream_latency_checks != 21)
            $fatal(1, "unexpected line-rate counters: %0d beats %0d results %0d latency checks",
                   stream_beats, stream_results, stream_latency_checks);
        if (stream_end_cycle - stream_start_cycle + 1 != stream_beats)
            $fatal(1, "unexpected source bubble during the line-rate block");
        if (stalls == 0 || blocked_ends == 0 || full_midframe_beats == 0 ||
            malformed_count == 0 || unsupported_count == 0 || stale_count == 0 || gap_count == 0)
            $fatal(1, "missing directed coverage");
        $display("PASS: %0d exact completions (%0d decoded, %0d unsupported, %0d malformed)",
                 outputs, decoded_count, unsupported_count, malformed_count);
        $display("Sequences: %0d gaps, %0d stale; flow control: %0d stalled outputs, %0d blocked ends, %0d full-buffer prefix beats",
                 gap_count, stale_count, stalls, blocked_ends, full_midframe_beats);
        $display("Line rate: %0d beats, %0d completions, %0d next-edge latency checks",
                 stream_beats, stream_results, stream_latency_checks);
        $fclose(tx);
        $fclose(rx);
        $finish;
    end
endmodule
