`timescale 1ns/1ps

module tb_eth_packet_arbiter;
    localparam integer PAYLOAD_BITS = 32, MAX_HI = 3;
    logic clk = 0;
    always #5 clk = !clk;
    logic rst = 1;
    logic [1:0] in_valid = 0, in_ready, in_last = 0;
    logic [63:0] in_data = 0;
    logic out_valid, out_ready = 0, out_lane;
    logic [31:0] out_data;
    eth_packet_arbiter #(.WIDTH(PAYLOAD_BITS),.HIGH_BURST(MAX_HI)) dut (.*);

    integer phase = 0, phase_cycle = 0, lifetime_cycle = 0;
    integer packet_no [0:1], word_no [0:1], budget = 0;
    integer target [0:1], retired = 0, holds = 0, gaps = 0;
    integer must_serve_low = 0, both_present = 0, one_hot_checks = 0;
    integer lock_lane = -1, desired, transfers;
    logic held = 0;
    logic [1:0] accepted = 0;
    logic [32:0] old_output;
    logic [31:0] random_state = 32'h47f51abc;

    // Step the fixed-seed pseudo-random generator used for repeatable test patterns.
    function automatic logic [31:0] random_next(input logic [31:0] value);
        logic [31:0] x;
        begin
            x = value ^ (value << 13);
            x = x ^ (x >> 17);
            random_next = x ^ (x << 5);
        end
    endfunction

    // Choose a reproducible payload length from lane and packet index.
    function automatic integer packet_length(input integer lane, input integer index);
        packet_length = 1 + ((17 * index + 7 * lane + phase) % 13);
    endfunction

    // Encode lane, packet, and beat position in the expected payload word.
    function automatic logic [31:0] word_value(input integer lane, input integer index,
                                               input integer offset);
        word_value = {8'(phase),8'(lane),8'(index),8'(offset)};
    endfunction

    always @(negedge clk) if (!rst) begin
        phase_cycle = phase_cycle + 1;
        random_state = random_next(random_state);
        out_ready = phase == 1 ? phase_cycle >= 9 : random_state[1:0] != 0;
        for (integer lane=0; lane<2; lane=lane+1) begin
            if (!in_valid[lane] || accepted[lane]) begin
                in_valid[lane] = packet_no[lane] < target[lane] &&
                    (phase != 1 || lane == 1 || phase_cycle >= 5) &&
                    (phase != 3 || random_state[8+lane*3 +: 3] > 2);
                in_data[32*lane +: 32] = word_value(lane,packet_no[lane],word_no[lane]);
                in_last[lane] = word_no[lane]+1 == packet_length(lane,packet_no[lane]);
            end
        end
    end

    always @(posedge clk) begin
        lifetime_cycle = lifetime_cycle + 1;
        accepted = in_ready & in_valid;
        if (lifetime_cycle > 20000)
            $fatal(1,"packet arbiter benchmark timed out");
        if (rst) begin
            packet_no[0] = 0;
            packet_no[1] = 0;
            word_no[0] = 0;
            word_no[1] = 0;
            budget = 0;
            lock_lane = -1;
            held = 0;
        end else begin
            if (in_ready == 2'b11)
                $fatal(1,"more than one input issued");
            one_hot_checks = one_hot_checks + 1;
            if (held && (!out_valid || {out_lane,out_data} !== old_output))
                $fatal(1,"stalled packet grant changed");
            held = out_valid && !out_ready;
            if (held) begin
                old_output = {out_lane,out_data};
                holds = holds + 1;
            end
            if (lock_lane >= 0 && !in_valid[lock_lane] && in_valid[1-lock_lane]) begin
                if (out_valid)
                    $fatal(1,"other source bypassed in-flight packet with a source bubble");
                gaps = gaps + 1;
            end
            if (out_valid) begin
                if (lock_lane >= 0)
                    desired = lock_lane;
                else if (in_valid[0] && in_valid[1]) begin
                    both_present = both_present + 1;
                    desired = budget >= MAX_HI ? 1 : 0;
                    if (desired == 1)
                        must_serve_low = must_serve_low + 1;
                end else begin
                    desired = in_valid[0] ? 0 : 1;
                end
                if (int'(out_lane) != desired)
                    $fatal(1,"packet arbiter did not honor its weighted grant model");
                if (out_data !== word_value(desired,packet_no[desired],word_no[desired]))
                    $fatal(1,"packet word left out of order");
                lock_lane = desired;
                if (out_ready) begin
                    if (!in_ready[desired])
                        $fatal(1,"winning lane did not transfer");
                    transfers = transfers + 1;
                    if (word_no[desired]+1 == packet_length(desired,packet_no[desired])) begin
                        retired = retired + 1;
                        word_no[desired] = 0;
                        packet_no[desired] = packet_no[desired]+1;
                        lock_lane = -1;
                        if (desired == 1)
                            budget = 0;
                        else if (budget < MAX_HI)
                            budget = budget + 1;
                    end else begin
                        word_no[desired] = word_no[desired]+1;
                    end
                end
            end
        end
    end

    // Run a packet arbitration phase to completion under its selected load mix.
    task automatic run_phase(input integer which, input integer high_count, input integer low_count);
        begin
            @(negedge clk);
            rst = 1;
            in_valid = 0;
            repeat (2) @(negedge clk);
            phase = which;
            phase_cycle = 0;
            target[0] = high_count;
            target[1] = low_count;
            rst = 0;
            while (packet_no[0] != high_count || packet_no[1] != low_count)
                @(negedge clk);
        end
    endtask

    initial begin
        transfers = 0;
        run_phase(1,1,1);
        run_phase(2,100,100);
        run_phase(3,80,80);
        if (retired != 362 || transfers < 2000 || holds < 100 || gaps < 100 ||
            both_present < 100 || must_serve_low < 30 || one_hot_checks < transfers)
            $fatal(1,"insufficient packet arbiter coverage");
        $display("PASS arbiter: %0d packets, %0d beats, %0d stalls, %0d locked source bubbles",
                 retired,transfers,holds,gaps);
        $finish;
    end
endmodule
