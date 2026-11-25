`timescale 1ns/1ps
`default_nettype none

module tb_dma_queue_table #(
    parameter bit ENFORCE_QUEUE_ISOLATION = 1'b0
);
    localparam integer QUEUES = 3;
    localparam integer RING_LOG = 2;
    localparam integer DEPTH = 1 << RING_LOG;
    localparam integer Q_W = 2;

    logic clk = 0;
    always #5 clk = ~clk;
    logic reset;
    logic cfg_valid, cfg_enable;
    wire cfg_ready, cfg_ack, cfg_ok;
    logic [Q_W-1:0] cfg_queue;
    logic [63:0] cfg_submission_base, cfg_completion_base, cfg_payload_low, cfg_payload_high;
    logic [31:0] cfg_seed, cfg_irq_delay;
    logic [7:0] cfg_irq_batch;

    logic host_valid;
    wire host_ready, host_ack, host_ok;
    logic [Q_W-1:0] host_queue;
    logic [31:0] host_submit_tail, host_complete_head;
    logic active, retire, immediate_irq;
    logic [Q_W-1:0] active_queue, selected, inspect_queue;
    logic [QUEUES-1:0] irq_ack;
    wire [QUEUES-1:0] irq, eligible;
    wire [63:0] selected_sq_addr, selected_cq_addr, selected_payload_low, selected_payload_high;
    wire inspect_enabled;
    wire [31:0] inspect_sq_head, inspect_sq_tail, inspect_cq_head, inspect_cq_tail;
    wire [7:0] inspect_irq_backlog;

    dma_queue_table #(
        .QUEUES(QUEUES), .RING_LOG(RING_LOG), .ENFORCE_QUEUE_ISOLATION(ENFORCE_QUEUE_ISOLATION)
    ) dut (.*);

    logic q_enabled [0:QUEUES-1];
    logic [63:0] sq [0:QUEUES-1], cq [0:QUEUES-1];
    logic [63:0] payload_l [0:QUEUES-1], payload_h [0:QUEUES-1];
    logic [31:0] submission_head [0:QUEUES-1], submission_tail [0:QUEUES-1];
    logic [31:0] completion_head [0:QUEUES-1], completion_tail [0:QUEUES-1];
    integer irq_threshold [0:QUEUES-1], irq_deadline [0:QUEUES-1];
    integer backlog [0:QUEUES-1], pending_since [0:QUEUES-1];
    logic expected_irq [0:QUEUES-1];
    integer clock_number = 0;
    integer q_retires [0:QUEUES-1];
    integer invalid_config = 0, invalid_host = 0, accepted_config = 0, accepted_host = 0;
    integer pressure = 0, cursor_wraps = 0, irq_seen = 0, config_concurrency = 0;
    integer admitted_same_cycle = 0;

    // Calculate modulo-2^32 cursor distance so wrap does not change occupancy.
    function automatic [31:0] distance(input logic [31:0] tail, head);
        distance = tail - head;
    endfunction

    // Check both the submission count and available completion credit.
    function automatic logic is_eligible(input integer q);
        is_eligible = q_enabled[q] && submission_head[q] != submission_tail[q] &&
                      distance(completion_tail[q], completion_head[q]) < DEPTH;
    endfunction

    // Test interval separation using half-open range endpoints.
    function automatic logic disjoint(
        input logic [64:0] first_low, first_high, second_low, second_high
    );
        disjoint = first_high <= second_low || second_high <= first_low;
    endfunction

    // Validate base alignment, address bounds and nonoverlapping rings.
    function automatic logic valid_layout(input integer configured_queue);
        logic [64:0] sq_start, sq_limit, cq_start, cq_limit, data_low, data_high;
        logic [64:0] candidate_start [0:2], candidate_end [0:2];
        logic [64:0] peer_start [0:2], peer_end [0:2];
        begin
            sq_start = {1'b0, cfg_submission_base};
            sq_limit = sq_start + DEPTH * 32;
            cq_start = {1'b0, cfg_completion_base};
            cq_limit = cq_start + DEPTH * 32;
            data_low = {1'b0, cfg_payload_low};
            data_high = {1'b0, cfg_payload_high};
            valid_layout = sq_start[4:0] == 0 && cq_start[4:0] == 0 &&
                           data_low[2:0] == 0 && data_high[2:0] == 0 &&
                           !sq_limit[64] && !cq_limit[64] && data_low < data_high &&
                           disjoint(sq_start, sq_limit, cq_start, cq_limit) &&
                           disjoint(sq_start, sq_limit, data_low, data_high) &&
                           disjoint(cq_start, cq_limit, data_low, data_high);
            if (ENFORCE_QUEUE_ISOLATION) begin
                candidate_start[0] = sq_start; candidate_end[0] = sq_limit;
                candidate_start[1] = cq_start; candidate_end[1] = cq_limit;
                candidate_start[2] = data_low; candidate_end[2] = data_high;
                for (int owner = 0; owner < QUEUES; owner++) begin
                    if (owner != configured_queue && q_enabled[owner]) begin
                        peer_start[0] = {1'b0, sq[owner]}; peer_end[0] = peer_start[0] + DEPTH*32;
                        peer_start[1] = {1'b0, cq[owner]}; peer_end[1] = peer_start[1] + DEPTH*32;
                        peer_start[2] = {1'b0, payload_l[owner]}; peer_end[2] = {1'b0, payload_h[owner]};
                        for (int candidate = 0; candidate < 3; candidate++)
                            for (int region = 0; region < 3; region++)
                                if (!disjoint(candidate_start[candidate], candidate_end[candidate],
                                              peer_start[region], peer_end[region]))
                                    valid_layout = 1'b0;
                    end
                end
            end
        end
    endfunction

    // Compare the public queue-table outputs with the reference arrays.
    task automatic check_ports;
        logic [63:0] expected_sq, expected_cq;
        integer q;
        begin
            for (integer i = 0; i < QUEUES; i = i + 1) begin
                if (eligible[i] !== is_eligible(i) || irq[i] !== expected_irq[i])
                    $fatal(1, "queue %0d eligibility or IRQ diverged cycle %0d", i, clock_number);
                if (q_enabled[i] && submission_head[i] != submission_tail[i] &&
                    distance(completion_tail[i], completion_head[i]) == DEPTH)
                    pressure++;
            end
            q = int'(inspect_queue);
            if (q < QUEUES && q_enabled[q]) begin
                if (!inspect_enabled ||
                    {inspect_sq_head, inspect_sq_tail, inspect_cq_head, inspect_cq_tail} !==
                    {submission_head[q], submission_tail[q], completion_head[q], completion_tail[q]} ||
                    inspect_irq_backlog !== 8'(backlog[q]))
                    $fatal(1, "inspect mismatch queue %0d cycle %0d", q, clock_number);
            end else if (inspect_enabled || inspect_sq_head || inspect_sq_tail ||
                         inspect_cq_head || inspect_cq_tail || inspect_irq_backlog) begin
                $fatal(1, "disabled or invalid queue leaked an inspection view");
            end
            q = int'(selected);
            if (q < QUEUES && q_enabled[q]) begin
                expected_sq = sq[q] + 32 * (submission_head[q] % DEPTH);
                expected_cq = cq[q] + 32 * (completion_tail[q] % DEPTH);
                if ({selected_sq_addr, selected_cq_addr, selected_payload_low, selected_payload_high}
                    !== {expected_sq, expected_cq, payload_l[q], payload_h[q]})
                    $fatal(1, "queue-selected address mismatch, queue %0d", q);
            end
        end
    endtask

    // Capture the current handshake, update the oracle and check post-edge outputs.
    task automatic step;
        logic will_cfg, will_host, accepted_c, accepted_h;
        logic completion, notify;
        integer c, h, r;
        int unsigned occupied;
        begin
            @(posedge clk);
            clock_number++;
            check_ports();
            will_cfg = cfg_valid && !active;
            will_host = host_valid && !will_cfg;
            c = int'(cfg_queue);
            h = int'(host_queue);
            r = int'(active_queue);
            accepted_c = 0;
            accepted_h = 0;
            if (will_cfg && c < QUEUES) begin
                accepted_c = (!q_enabled[c] ||
                              (submission_head[c] == submission_tail[c] &&
                               completion_head[c] == completion_tail[c])) &&
                             (!cfg_enable || valid_layout(c));
            end
            if (will_host && h < QUEUES && q_enabled[h]) begin
                occupied = distance(submission_tail[h], submission_head[h]);
                accepted_h = occupied <= DEPTH &&
                             distance(host_submit_tail, submission_tail[h]) <= DEPTH - occupied &&
                             distance(host_complete_head, completion_head[h]) <=
                             distance(completion_tail[h], completion_head[h]);
            end
            if (cfg_ready !== !active || host_ready !== !will_cfg)
                $fatal(1, "priority mismatch around simultaneous host/config requests");
            if (will_cfg) begin
                if (accepted_c) accepted_config++;
                else invalid_config++;
                if (host_valid) config_concurrency++;
            end
            if (will_host) begin
                if (accepted_h) accepted_host++;
                else invalid_host++;
            end
            if (accepted_c) begin
                q_enabled[c] = cfg_enable;
                submission_head[c] = cfg_seed;
                submission_tail[c] = cfg_seed;
                completion_head[c] = cfg_seed;
                completion_tail[c] = cfg_seed;
                sq[c] = cfg_submission_base;
                cq[c] = cfg_completion_base;
                payload_l[c] = cfg_payload_low;
                payload_h[c] = cfg_payload_high;
                irq_threshold[c] = int'(cfg_irq_batch);
                irq_deadline[c] = int'(cfg_irq_delay);
            end
            if (accepted_h) begin
                if (host_submit_tail < submission_tail[h] || host_complete_head < completion_head[h])
                    cursor_wraps++;
                submission_tail[h] = host_submit_tail;
                completion_head[h] = host_complete_head;
            end
            if (retire) begin
                if (r >= QUEUES || !active || !q_enabled[r])
                    $fatal(1, "invalid retirement stimulus");
                if (accepted_h && h == r) admitted_same_cycle++;
                if (submission_head[r] == '1 || completion_tail[r] == '1)
                    cursor_wraps++;
                submission_head[r]++;
                completion_tail[r]++;
                q_retires[r]++;
            end
            for (integer slot = 0; slot < QUEUES; slot = slot + 1) begin
                completion = retire && r == slot;
                if ((accepted_c && c == slot) || irq_ack[slot]) begin
                    backlog[slot] = 0;
                    expected_irq[slot] = 0;
                end
                if (!(accepted_c && c == slot)) begin
                    if (completion) begin
                        if (backlog[slot] == 0) pending_since[slot] = clock_number;
                        if (backlog[slot] < 255) backlog[slot]++;
                    end
                    notify = (completion && immediate_irq) ||
                             (backlog[slot] != 0 &&
                              ((irq_threshold[slot] > 0 && backlog[slot] >= irq_threshold[slot]) ||
                               (irq_deadline[slot] > 0 &&
                                clock_number - pending_since[slot] + 1 >= irq_deadline[slot])));
                    if (notify && !expected_irq[slot]) irq_seen++;
                    expected_irq[slot] = expected_irq[slot] || notify;
                end
            end
            #1;
            if ({cfg_ack, cfg_ok, host_ack, host_ok} !== {will_cfg, accepted_c, will_host, accepted_h})
                $fatal(1, "ack/result mismatch cycle %0d", clock_number);
            check_ports();
        end
    endtask

    // Deassert request and interrupt inputs before the next directed step.
    task automatic idle_inputs;
        begin
            cfg_valid = 0;
            host_valid = 0;
            active = 0;
            retire = 0;
            immediate_irq = 0;
            irq_ack = 0;
        end
    endtask

    // Present a queue-table configuration request with its acknowledgment expectation.
    task automatic drive_config(
        input integer q,
        input logic enable,
        input logic [31:0] seed,
        input integer batch, delay_cycles
    );
        begin
            cfg_valid = 1;
            cfg_queue = Q_W'(q);
            cfg_enable = enable;
            cfg_submission_base = 64'h1000 + 64'(q) * 64'h4000;
            cfg_completion_base = cfg_submission_base + 64'h800;
            cfg_payload_low = cfg_submission_base + 64'h1000;
            cfg_payload_high = cfg_submission_base + 64'h1f00;
            cfg_seed = seed;
            cfg_irq_batch = 8'(batch);
            cfg_irq_delay = 32'(delay_cycles);
        end
    endtask

    // Present host cursor updates for one queue with expected validity.
    task automatic drive_host(
        input integer q,
        input logic [31:0] tail, head
    );
        begin
            host_valid = 1;
            host_queue = Q_W'(q);
            host_submit_tail = tail;
            host_complete_head = head;
            inspect_queue = Q_W'(q);
        end
    endtask

    // Run fixed full, empty, wrap, overlap and acknowledgment cases.
    task automatic direct;
        begin
            @(negedge clk);
            drive_config(3, 1, 0, 1, 1); step();
            @(negedge clk);
            drive_config(0, 1, 0, 1, 1); cfg_submission_base |= 1; step();
            @(negedge clk);
            drive_config(0, 1, 0, 1, 1); cfg_payload_high = cfg_payload_low; step();
            @(negedge clk);
            drive_config(0, 1, 0, 1, 1); cfg_completion_base = cfg_submission_base + 32; step();
            @(negedge clk);
            drive_config(0, 1, 0, 1, 1); cfg_payload_low = cfg_completion_base; step();
            @(negedge clk);
            drive_config(0, 1, 0, 1, 1); cfg_submission_base = 64'hffff_ffff_ffff_ffe0; step();
            @(negedge clk);
            drive_config(0, 1, 32'hffff_fffe, 3, 0);
            drive_host(0, 2, 32'hffff_fffe);
            step();
            @(negedge clk);
            cfg_valid = 0; step();
            @(negedge clk);
            drive_host(0, 3, 32'hffff_fffe); step();
            @(negedge clk);
            drive_host(0, 1, 32'hffff_fffe); step();
            @(negedge clk);
            drive_host(0, 2, 32'hffff_ffff); step();
            @(negedge clk);
            host_valid = 0; drive_config(0, 0, 8, 0, 0); step();
            for (integer i = 0; i < DEPTH; i = i + 1) begin
                @(negedge clk);
                idle_inputs(); active = 1; active_queue = 0; selected = 0; retire = 1; step();
            end
            @(negedge clk);
            idle_inputs(); drive_host(0, 4, 32'hffff_fffe); step();
            @(negedge clk);
            drive_host(0, 4, 32'hffff_ffff); irq_ack[0] = 1; step();
            @(negedge clk);
            idle_inputs(); active = 1; active_queue = 0; retire = 1; immediate_irq = 1; step();
            @(negedge clk);
            idle_inputs(); drive_config(1, 1, 48, 0, 3); step();
            @(negedge clk);
            idle_inputs(); drive_config(2, 1, 89, 2, 6); step();
        end
    endtask

    // Mutate each candidate region against each occupied peer interval.
    task automatic isolate_pairs;
        logic [63:0] end_of_owner [0:2];
        int rejected_pairs = 0;
        end_of_owner[0] = sq[0] + DEPTH*32;
        end_of_owner[1] = cq[0] + DEPTH*32;
        end_of_owner[2] = payload_h[0];
        selected = 2;
        inspect_queue = 2;
        for (int candidate = 0; candidate < 3; candidate++) begin
            for (int owner_region = 0; owner_region < 3; owner_region++) begin
                @(negedge clk);
                idle_inputs();
                drive_config(2,1,9876,3,6);
                case (candidate)
                    0: cfg_submission_base = end_of_owner[owner_region] - 32;
                    1: cfg_completion_base = end_of_owner[owner_region] - 32;
                    2: begin
                        cfg_payload_low = end_of_owner[owner_region] - 8;
                        cfg_payload_high = end_of_owner[owner_region] + 8;
                    end
                endcase
                step();
                if (!cfg_ack || cfg_ok || inspect_sq_head != 89 || inspect_cq_tail != 89)
                    $fatal(1, "overlap %0d/%0d changed an active layout",candidate,owner_region);
                rejected_pairs++;
            end
        end
        @(negedge clk);
        idle_inputs();
        drive_config(2,1,1234,1,2);
        cfg_submission_base = end_of_owner[0];
        cfg_completion_base = end_of_owner[1];
        cfg_payload_low = end_of_owner[2];
        cfg_payload_high = end_of_owner[2] + 8;
        step();
        if (!cfg_ok || inspect_sq_head != 1234)
            $fatal(1, "legal half-open endpoint adjacency was rejected");
        @(negedge clk);
        // Replacing one's own unchanged layout does not conflict with itself.
        cfg_seed = 1235;
        step();
        if (!cfg_ok || inspect_sq_head != 1235) $fatal(1, "own layout blocked replacement");
        @(negedge clk);
        drive_config(0,0,18,0,0);
        step();
        if (cfg_ok) $fatal(1, "owner with outstanding slots was disabled");
        @(negedge clk);
        drive_config(2,1,211,1,2);
        cfg_payload_low = sq[0];
        cfg_payload_high = sq[0] + 8;
        step();
        if (cfg_ok) $fatal(1, "rejected disable released ownership");
        @(negedge clk);
        drive_config(1,0,18,0,0);
        step();
        if (!cfg_ok) $fatal(1, "drained owner did not release its regions");
        @(negedge clk);
        drive_config(2,1,1337,1,2);
        cfg_submission_base = sq[1];
        cfg_completion_base = cq[1];
        cfg_payload_low = payload_l[1];
        cfg_payload_high = payload_h[1];
        step();
        if (!cfg_ok) $fatal(1, "disabled owner's regions could not be reused");
        @(negedge clk);
        drive_config(2,1,89,2,6);
        step();
        @(negedge clk);
        drive_config(1,1,48,0,3);
        step();
        $display("PASS DMA isolation: %0d pairwise conflicts, adjacency, own replacement and handoff", rejected_pairs);
    endtask

    // Advance the deterministic pseudo-random sequence used as fixture data.
    function automatic [31:0] scramble(input logic [31:0] value);
        logic [31:0] x;
        begin
            x = value ^ (value << 13);
            x = x ^ (x >> 17);
            scramble = x ^ (x << 5);
        end
    endfunction

    logic [31:0] seed = 32'hbd59_005a;
    integer rr_cursor = 0;
    integer q, hit, pending_count, cq_count, advance, release_count;
    initial begin
        reset = 1;
        idle_inputs();
        cfg_queue = 0;
        host_queue = 0;
        active_queue = 0;
        selected = 0;
        inspect_queue = 0;
        for (integer idx = 0; idx < QUEUES; idx = idx + 1) begin
            q_enabled[idx] = 0;
            sq[idx] = 0; cq[idx] = 0;
            payload_l[idx] = 0; payload_h[idx] = 0;
            submission_head[idx] = 0; submission_tail[idx] = 0;
            completion_head[idx] = 0; completion_tail[idx] = 0;
            irq_threshold[idx] = 0; irq_deadline[idx] = 0;
            backlog[idx] = 0; pending_since[idx] = 0;
            expected_irq[idx] = 0; q_retires[idx] = 0;
        end
        repeat (3) @(negedge clk);
        reset = 0;
        direct();
        if (ENFORCE_QUEUE_ISOLATION) isolate_pairs();
        for (integer t = 0; t < 2200; t = t + 1) begin
            @(negedge clk);
            idle_inputs();
            seed = scramble(seed);
            q = int'(seed[1:0]);
            inspect_queue = Q_W'(seed[3:2]);
            hit = -1;
            for (integer distance_rr = 0; distance_rr < QUEUES; distance_rr++) begin
                int candidate;
                candidate = (rr_cursor + distance_rr) % QUEUES;
                if (is_eligible(candidate) && hit < 0) hit = candidate;
            end
            if (hit >= 0 && seed[4]) begin
                active = 1;
                active_queue = Q_W'(hit);
                selected = Q_W'(hit);
                retire = 1;
                rr_cursor = (hit + 1) % QUEUES;
            end
            immediate_irq = seed[7:5] == 0;
            irq_ack = seed[8] ? seed[11:9] : '0;
            if (q < QUEUES && q_enabled[q]) begin
                pending_count = int'(distance(submission_tail[q], submission_head[q]));
                cq_count = int'(distance(completion_tail[q], completion_head[q]));
                advance = int'(seed[15:12]) % (DEPTH - pending_count + 1);
                release_count = int'(seed[19:16]) % (cq_count + 1);
                if (seed[22:20] == 0) advance = DEPTH + 1;
                if (seed[25:23] == 0) release_count = cq_count + 1;
                drive_host(q, submission_tail[q] + 32'(advance), completion_head[q] + 32'(release_count));
            end else if (q == 3 && seed[26]) begin
                drive_host(q, seed, seed ^ 32'h783c_aa01);
            end
            if (seed[31:27] == 0)
                drive_config((q + 1) % QUEUES, 1, seed, q + 1, q + 2);
            step();
        end
        for (integer idx = 0; idx < QUEUES; idx = idx + 1)
            if (q_retires[idx] < 30)
                $fatal(1, "eligible round-robin stimulus missed queue %0d", idx);
        if (invalid_config < 8 || invalid_host < 200 || accepted_host < 600 ||
            cursor_wraps < 3 || pressure < 30 || irq_seen < 100 ||
            config_concurrency < 5 || admitted_same_cycle < 30)
            $fatal(1, "queue boundary coverage thresholds were not met");
        $display("PASS DMA queue table: retire counts %0d/%0d/%0d, host accept/reject %0d/%0d, config accept/reject %0d/%0d",
                 q_retires[0], q_retires[1], q_retires[2], accepted_host, invalid_host,
                 accepted_config, invalid_config);
        $display("uint32 wraps=%0d CQ full=%0d IRQ edges=%0d cfg/host priority=%0d concurrent retire/update=%0d",
                 cursor_wraps, pressure, irq_seen, config_concurrency, admitted_same_cycle);
        $finish;
    end

    initial begin
        #1000000;
        $fatal(1, "queue test watchdog");
    end
endmodule

`default_nettype wire
