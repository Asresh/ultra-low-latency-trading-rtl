`timescale 1ns/1ps
`default_nettype none

module tb_matching_transport;
    localparam integer PORTS = 3;
    localparam integer WIDTH = 24;
    localparam integer CYCLES = 3100;

    logic clk = 1'b0;
    always #5 clk = !clk;
    logic rst = 1'b1;
    logic [PORTS-1:0] in_valid = '0;
    wire [PORTS-1:0] in_ready;
    logic [WIDTH-1:0] in_data [0:PORTS-1];
    wire [PORTS-1:0] lane_valid, lane_pop;
    wire [PORTS*WIDTH-1:0] lane_payload;
    wire sink_valid;
    logic sink_ready = 1'b0;
    wire [WIDTH-1:0] sink_data;

    for (genvar lane = 0; lane < PORTS; lane++) begin : gen_fifo
        matching_fifo #(.DEPTH(lane+1), .WIDTH(WIDTH)) dut (
            .clk(clk), .rst(rst), .s_valid(in_valid[lane]), .s_ready(in_ready[lane]),
            .s_payload(in_data[lane]), .m_valid(lane_valid[lane]), .m_ready(lane_pop[lane]),
            .m_payload(lane_payload[lane*WIDTH+:WIDTH])
        );
    end

    matching_arbiter #(.PORTS(PORTS), .WIDTH(WIDTH)) arbiter (
        .clk(clk), .rst(rst), .s_valid(lane_valid), .s_ready(lane_pop), .s_payload(lane_payload),
        .m_valid(sink_valid), .m_ready(sink_ready), .m_payload(sink_data)
    );

    logic [WIDTH-1:0] expected [0:PORTS-1][0:CYCLES-1];
    integer head [0:PORTS-1];
    integer tail [0:PORTS-1];
    integer pushed [0:PORTS-1];
    integer popped [0:PORTS-1];
    integer full_cycles [0:PORTS-1];
    integer simultaneous [0:PORTS-1];
    integer sequence_counter [0:PORTS-1];
    integer first, locked, stalled_cycles, reset_nonempty, overlap;
    logic [PORTS-1:0] keep_driving;
    logic [31:0] rng = 32'h8492_610b;

    // Advance the testbench PRNG and draw a deterministic value below limit.
    function automatic integer roll(input integer limit);
        rng = rng ^ (rng << 13);
        rng = rng ^ (rng >> 17);
        rng = rng ^ (rng << 5);
        roll = int'(rng % $unsigned(limit));
    endfunction

    // Clear all expected queues, grants, and held output state when the DUT resets.
    task automatic reset_scoreboard;
        first = 0;
        locked = -1;
        keep_driving = '0;
        for (integer lane = 0; lane < PORTS; lane++) begin
            head[lane] = 0;
            tail[lane] = 0;
        end
    endtask

    // Offer per-lane randomized beats while leaving any stalled beat unchanged.
    task automatic generate_sources(input integer step);
        for (integer lane = 0; lane < PORTS; lane++) begin
            if (rst) begin
                in_valid[lane] = 1'b0;
                in_data[lane] = '0;
            end else if (!keep_driving[lane]) begin
                if (step < 25 || (step < CYCLES - 36 && roll(10) > lane+1)) begin
                    in_valid[lane] = 1'b1;
                    in_data[lane] = {8'(lane), 16'(sequence_counter[lane])};
                    sequence_counter[lane]++;
                end else begin
                    in_valid[lane] = 1'b0;
                    in_data[lane] = WIDTH'(roll(105103));
                end
            end
        end
    endtask

    // Find the next eligible egress lane using the scoreboard's round-robin cursor.
    function automatic integer expected_lane;
        integer candidate;
        if (locked >= 0) begin
            expected_lane = locked;
        end else begin
            expected_lane = -1;
            for (integer offset = 0; offset < PORTS; offset++) begin
                candidate = (first + offset) % PORTS;
                if (expected_lane < 0 && head[candidate] < tail[candidate])
                    expected_lane = candidate;
            end
        end
    endfunction

    // Verify one edge of ingress, egress, and occupancy before updating the reference queues.
    task automatic check_cycle(input integer step);
        integer selected, occupancy, pushes;
        logic [PORTS-1:0] expected_grant, accepted;
        logic pop;
        if (rst) begin
            if (in_ready !== '0 || sink_valid !== 1'b0 || lane_pop !== '0)
                $fatal(1, "active transport while reset at step %0d", step);
            for (integer lane = 0; lane < PORTS; lane++) begin
                if (head[lane] < tail[lane])
                    reset_nonempty++;
            end
            reset_scoreboard();
            return;
        end

        selected = expected_lane();
        expected_grant = '0;
        accepted = '0;
        if (sink_valid !== (selected >= 0))
            $fatal(1, "arbiter valid mismatch at step %0d", step);
        if (selected >= 0) begin
            if (sink_data !== expected[selected][head[selected]])
                $fatal(1, "wrong arbiter data at step %0d from lane %0d", step, selected);
            if (sink_ready) begin
                expected_grant[selected] = 1'b1;
                locked = -1;
                first = (selected + 1) % PORTS;
            end else begin
                locked = selected;
                stalled_cycles++;
            end
        end
        if (lane_pop !== expected_grant)
            $fatal(1, "incorrect grant at step %0d", step);

        pushes = 0;
        for (integer lane = 0; lane < PORTS; lane++) begin
            occupancy = tail[lane] - head[lane];
            if (in_ready[lane] !== (occupancy < lane+1) || lane_valid[lane] !== (occupancy > 0))
                $fatal(1, "flow control mismatch at step %0d lane %0d", step, lane);
            if (occupancy == lane+1)
                full_cycles[lane]++;
            if (occupancy > 0 && lane_payload[lane*WIDTH+:WIDTH] !== expected[lane][head[lane]])
                $fatal(1, "queue head mismatch at step %0d lane %0d", step, lane);
            accepted[lane] = in_valid[lane] && in_ready[lane];
            pop = lane_pop[lane];
            if (accepted[lane] && pop)
                simultaneous[lane]++;
            if (pop) begin
                head[lane]++;
                popped[lane]++;
            end
            if (accepted[lane]) begin
                expected[lane][tail[lane]] = in_data[lane];
                tail[lane]++;
                pushed[lane]++;
                pushes++;
            end
        end
        if (pushes > 1)
            overlap++;
        keep_driving = in_valid & ~accepted;
    endtask

    initial begin
        reset_scoreboard();
        stalled_cycles = 0;
        reset_nonempty = 0;
        overlap = 0;
        for (integer lane = 0; lane < PORTS; lane++) begin
            pushed[lane] = 0;
            popped[lane] = 0;
            full_cycles[lane] = 0;
            simultaneous[lane] = 0;
            sequence_counter[lane] = 0;
            in_data[lane] = '0;
        end

        for (integer step = 0; step < CYCLES; step++) begin
            @(negedge clk);
            rst = (step < 2 || step == 17 || step == 1500);
            sink_ready = !rst && step > 20 &&
                (step >= CYCLES-36 || (step % 99 < 82 && roll(11) < 8));
            generate_sources(step);
            @(posedge clk);
            check_cycle(step);
        end
        for (integer lane = 0; lane < PORTS; lane++) begin
            if (head[lane] != tail[lane] || pushed[lane] < 10 || popped[lane] < 10 ||
                full_cycles[lane] == 0)
                $fatal(1, "lane %0d did not exercise or drain its queue", lane);
            if ((lane == 0 && simultaneous[lane] != 0) || (lane != 0 && simultaneous[lane] == 0))
                $fatal(1, "lane %0d simultaneous push/pop coverage mismatch", lane);
        end
        if (stalled_cycles == 0 || reset_nonempty == 0 || overlap == 0)
            $fatal(1, "missing stall, reset or independent ingress coverage");
        $display("PASS FIFO/arbiter depths=1/2/3 cycles=%0d stalls=%0d overlap=%0d nonempty_reset_lanes=%0d",
            CYCLES, stalled_cycles, overlap, reset_nonempty);
        $finish;
    end
endmodule

`default_nettype wire
