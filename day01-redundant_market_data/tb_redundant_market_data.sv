`timescale 1ns/1ps

module tb_redundant_market_data;
    localparam integer OP_CONFIG = 0, OP_BURST = 1, OP_WAIT = 2, OP_STATUS = 3,
                       OP_REQUEST = 4, OP_FLOW = 5, OP_STAT = 6, OP_EVENTS = 7,
                       OP_NO_REQUEST = 8, OP_BENCHMARK = 9, OP_STAT_PEEK = 10,
                       OP_OCCUPANCY_PEAK = 11;

    logic clk = 0;
    always #5 clk = !clk;

    logic rst = 1;
    logic cfg_valid = 0, cfg_ready;
    logic [79:0] cfg_session = 0;
    logic [63:0] cfg_sequence = 0;
    logic [1:0] feed_valid = 0, feed_ready;
    logic [127:0] feed_data = 0;
    logic [15:0] feed_keep = 0;
    logic [1:0] feed_first = 0, feed_last = 0;
    logic event_valid, event_ready = 0;
    logic [587:0] event_data;
    logic request_valid, request_ready = 0;
    logic [159:0] request_data;
    logic [63:0] expected_sequence, high_water;
    logic gap_pending, session_end_seen;
    logic [5:0] reorder_occupancy;
    logic stats_snapshot = 0;
    logic [3:0] stats_index = 0;
    logic [31:0] stats_data;
    logic [5:0] stats_peak;

    `ifdef DEFAULT_GEOMETRY
    localparam logic SMALL_TEST = 0;
    redundant_market_data dut (.*);
    `else
    localparam logic SMALL_TEST = 1;
    redundant_market_data #(
        .PACKET_BYTES(256), .MESSAGE_BYTES(64), .BLOCKS(16),
        .WINDOW(8), .GAP_TICKS(48), .REQUEST_MAX(4)
    ) dut (.*);
    `endif

    integer cycle = 0, step = 0, event_count = 0, request_count = 0;
    integer input_count [0:1];
    integer stall_count = 0, request_stall_count = 0, ingress_stall_count = 0;
    integer first_input_cycle = 0, last_input_cycle = 0, first_event_cycle = 0;
    integer simultaneous_count = 0, packet_end_cycle [0:1];
    integer event_mode = 0, event_checks = 0, request_checks = 0, status_checks = 0;
    integer program_file, output_file, wire_file [0:1];
    integer opcode, cmd_rc, event_rc;
    logic [159:0] a, b, c, d, e;
    logic event_was_stalled = 0, request_was_stalled = 0;
    logic [587:0] held_event, golden;
    logic [159:0] held_request;
    logic [31:0] ready_random = 32'h256b31b8;
    string location;

    // Advance the deterministic testbench PRNG used for source and sink timing.
    function automatic logic [31:0] xorshift(input logic [31:0] previous);
        logic [31:0] part;
        begin
            part = previous ^ previous << 13;
            part = part ^ part >> 17;
            xorshift = part ^ part << 5;
        end
    endfunction

    always @(negedge clk) begin
        ready_random = xorshift(ready_random);
        case (event_mode)
            1: event_ready = 1;
            2: event_ready = ready_random[2:0] >= 3;
            default: event_ready = 0;
        endcase
    end

    // Observe transfers before the design's edge-triggered state changes.
    always @(posedge clk) begin
        cycle = cycle + 1;
        if (cycle > 250000)
            $fatal(1, "testbench timeout in step %0d (opcode %0d)", step, opcode);
        if (rst || cfg_valid) begin
            event_was_stalled = 0;
            request_was_stalled = 0;
        end else begin
            if (event_was_stalled && (!event_valid || event_data !== held_event))
                $fatal(1, "event unstable at cycle %0d", cycle);
            if (request_was_stalled && (!request_valid || request_data !== held_request))
                $fatal(1, "request unstable at cycle %0d", cycle);
            event_was_stalled = event_valid && !event_ready;
            request_was_stalled = request_valid && !request_ready;
            if (event_was_stalled) begin
                held_event = event_data;
                stall_count = stall_count + 1;
            end
            if (request_was_stalled) begin
                held_request = request_data;
                request_stall_count = request_stall_count + 1;
            end
            for (integer lane = 0; lane < 2; lane = lane + 1) begin
                if (feed_valid[lane] && !feed_ready[lane])
                    ingress_stall_count = ingress_stall_count + 1;
                if (feed_valid[lane] && feed_ready[lane]) begin
                    input_count[lane] = input_count[lane] + 1;
                    if (first_input_cycle == 0)
                        first_input_cycle = cycle;
                    last_input_cycle = cycle;
                    if (feed_last[lane])
                        packet_end_cycle[lane] = cycle;
                end
            end
            if (&(feed_valid & feed_ready))
                simultaneous_count = simultaneous_count + 1;
            if (event_valid && event_ready) begin
                if (first_event_cycle == 0)
                    first_event_cycle = cycle;
                event_rc = $fscanf(output_file, "%h", golden);
                if (event_rc != 1 || event_data !== golden)
                    $fatal(1, "event %0d step %0d cycle %0d mismatch\nreceived %h\nexpected %h",
                           event_count, step, cycle, event_data, golden);
                event_count = event_count + 1;
            end
            if (request_valid && request_ready)
                request_count = request_count + 1;
        end
    end

    // Send the requested number of stored beats on one lane while respecting its ready signal.
    task automatic drive_lane(input integer lane, input integer total, input integer profile);
        logic [31:0] random_bits;
        logic [63:0] data_value;
        logic [7:0] keep_value;
        logic first_value, last_value;
        integer rc;
        begin
            random_bits = 32'hb6c48fe1 + 32'(step + 10801 * lane);
            if (profile == 2 && lane == 0)
                repeat (3) @(negedge clk);
            for (integer beat = 0; beat < total; beat = beat + 1) begin
                rc = $fscanf(wire_file[lane], "%h %h %d %d",
                             data_value, keep_value, first_value, last_value);
                if (rc != 4)
                    $fatal(1, "bad input vector for lane %0d at step %0d, item %0d",
                           lane, step, beat);
                @(negedge clk);
                random_bits = xorshift(random_bits);
                if (profile != 0 && random_bits[1:0] == 0) begin
                    feed_valid[lane] = 0;
                    @(negedge clk);
                end
                feed_data[64*lane +: 64] = data_value;
                feed_keep[8*lane +: 8] = keep_value;
                feed_first[lane] = first_value;
                feed_last[lane] = last_value;
                feed_valid[lane] = 1;
                do @(posedge clk); while (!feed_ready[lane]);
            end
            if (total != 0) begin
                @(negedge clk);
                feed_valid[lane] = 0;
            end
        end
    endtask

    // Install a session identifier and initial sequence, then wait for the control handshake.
    task automatic configure(input logic [79:0] session, input logic [63:0] next_seq);
        begin
            @(negedge clk);
            cfg_session = session;
            cfg_sequence = next_seq;
            cfg_valid = 1;
            do @(posedge clk); while (!cfg_ready);
            @(negedge clk);
            cfg_valid = 0;
        end
    endtask

    // Check a recovery request and deliberately stall it for hold_clocks if requested.
    task automatic compare_request(input logic [159:0] desired, input integer hold_clocks);
        integer before_count;
        begin
            @(negedge clk);
            if (request_valid !== 1'b1 || request_data !== desired)
                $fatal(1, "request step %0d: got %0d %h, expected %h",
                       step, request_valid, request_data, desired);
            for (integer hold = 0; hold < hold_clocks; hold = hold + 1) begin
                @(negedge clk);
                if (!request_valid || request_data !== desired)
                    $fatal(1, "pending request changed before readiness at step %0d", step);
            end
            before_count = request_count;
            request_ready = 1;
            @(negedge clk);
            request_ready = 0;
            if (request_count != before_count + 1)
                $fatal(1, "request did not transfer exactly once at step %0d", step);
            request_checks = request_checks + 1;
        end
    endtask

    // Read the selected statistics register and assert its expected counter value.
    task automatic compare_stat(input logic [3:0] index, input logic [31:0] desired);
        begin
            @(negedge clk);
            stats_index = index;
            stats_snapshot = 1;
            @(negedge clk);
            stats_snapshot = 0;
            if (stats_data !== desired)
                $fatal(1, "statistics step %0d index %0d: got %0d expected %0d (peak %0d)",
                       step, index, stats_data, desired, stats_peak);
        end
    endtask

    // Compare measured terminal edge timing and dual-lane overlap with the programmed bounds.
    task automatic compare_benchmark(input integer terminal_span, input integer event_latency,
                                     input integer ingress_span, input integer together);
        begin
            if (packet_end_cycle[0] - packet_end_cycle[1] != terminal_span ||
                first_event_cycle - packet_end_cycle[0] != event_latency ||
                last_input_cycle - first_input_cycle + 1 != ingress_span ||
                simultaneous_count != together)
                $fatal(1, "cycle budget changed: end skew %0d, event latency %0d, ingress %0d, simultaneous %0d",
                       packet_end_cycle[0] - packet_end_cycle[1],
                       first_event_cycle - packet_end_cycle[0],
                       last_input_cycle - first_input_cycle + 1, simultaneous_count);
            $display("Dual-lane benchmark: %0d simultaneous input beats, %0d cycles final-beat to event",
                     simultaneous_count, first_event_cycle - packet_end_cycle[0]);
        end
    endtask

    initial begin
        if (!$value$plusargs("vectors=%s", location))
            location = "build";
        program_file = $fopen({location, "/program.txt"}, "r");
        output_file = $fopen({location, "/golden.txt"}, "r");
        wire_file[0] = $fopen({location, "/feed0.txt"}, "r");
        wire_file[1] = $fopen({location, "/feed1.txt"}, "r");
        if (program_file == 0 || output_file == 0 || wire_file[0] == 0 || wire_file[1] == 0)
            $fatal(1, "could not open vector suite at %s", location);
        input_count[0] = 0;
        input_count[1] = 0;
        packet_end_cycle[0] = 0;
        packet_end_cycle[1] = 0;
        repeat (3) @(negedge clk);
        rst = 0;

        while (!$feof(program_file)) begin
            cmd_rc = $fscanf(program_file, "%d %h %h %h %h %h", opcode, a, b, c, d, e);
            if (cmd_rc == 6) begin
                step = step + 1;
                case (opcode)
                    OP_CONFIG: configure(a[79:0], b[63:0]);
                    OP_BURST: begin
                        fork
                            drive_lane(0, int'(a), int'(c));
                            drive_lane(1, int'(b), int'(c));
                        join
                    end
                    OP_WAIT: repeat (int'(a)) @(negedge clk);
                    OP_STATUS: begin
                        if ({expected_sequence, high_water, gap_pending,
                             session_end_seen, reorder_occupancy} !==
                            {a[63:0], b[63:0], c[0], d[0], e[5:0]})
                            $fatal(1, "status step %0d: expected=%h high=%h gap=%b end=%b occupancy=%0d\n  desired: %h %h %b %b %0d",
                                   step, expected_sequence, high_water, gap_pending,
                                   session_end_seen, reorder_occupancy,
                                   a[63:0], b[63:0], c[0], d[0], e[5:0]);
                        status_checks = status_checks + 1;
                    end
                    OP_REQUEST: compare_request(a, int'(b));
                    OP_FLOW: event_mode = int'(a);
                    OP_STAT: compare_stat(a[3:0], b[31:0]);
                    OP_STAT_PEEK: begin
                        @(negedge clk);
                        stats_index = a[3:0];
                        @(negedge clk);
                        if (stats_data !== b[31:0])
                            $fatal(1, "counter snapshot changed without a capture at step %0d", step);
                    end
                    OP_OCCUPANCY_PEAK: begin
                        if (stats_peak < a[5:0])
                            $fatal(1, "window peak %0d below expected %0d at step %0d",
                                   stats_peak, a[5:0], step);
                    end
                    OP_EVENTS: begin
                        if (event_count != int'(a))
                            $fatal(1, "completion step %0d: got %0d, expected %0d",
                                   step, event_count, int'(a));
                        event_checks = event_checks + 1;
                    end
                    OP_NO_REQUEST: begin
                        if (request_valid !== 1'b0)
                            $fatal(1, "unwanted request at step %0d: %h", step, request_data);
                    end
                    OP_BENCHMARK: compare_benchmark(int'(a), int'(b), int'(c), int'(d));
                    default: $fatal(1, "unrecognized program opcode %0d", opcode);
                endcase
            end else if (!$feof(program_file)) begin
                $fatal(1, "malformed program row after step %0d", step);
            end
        end

        repeat (15) @(negedge clk);
        if ($fscanf(output_file, "%h", golden) == 1 || event_valid)
            $fatal(1, "expected events left after %0d comparisons", event_count);
        if (SMALL_TEST && (event_checks < 100 || status_checks < 100 || request_checks < 3 ||
            stall_count == 0 || request_stall_count == 0 || ingress_stall_count == 0 ||
            input_count[0] == 0 || input_count[1] == 0))
            $fatal(1, "test did not reach its coverage counters");
        $display("PASS: %0d exact events, %0d request transfers, %0d status checks",
                 event_count, request_count, status_checks);
        $display("Flow: %0d/%0d beats A/B, stalls event/request/ingress = %0d/%0d/%0d",
                 input_count[0], input_count[1], stall_count, request_stall_count, ingress_stall_count);
        $fclose(program_file);
        $fclose(output_file);
        $fclose(wire_file[0]);
        $fclose(wire_file[1]);
        $finish;
    end
endmodule
