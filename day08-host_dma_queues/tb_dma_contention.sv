`timescale 1ns/1ps
`default_nettype none
`include "dma_protocol.svh"

module tb_dma_contention;
    localparam integer QUEUES = 3, RING_LOG = 2, DEPTH = 1<<RING_LOG;
    localparam integer Q_W = $clog2(QUEUES), MAX_BURST = 4;
    localparam integer LIMIT = 65000;
    localparam logic [63:0] PAY_LOW = 64'h8000, PAY_HIGH = 64'h9000;

    logic clk = 1'b0;
    always #5 clk = !clk;
    logic reset = 1'b1;
    logic cfg_valid = 1'b0, cfg_enable = 1'b0;
    wire cfg_ready, cfg_ack, cfg_ok;
    logic [Q_W-1:0] cfg_queue = '0;
    logic [63:0] cfg_submission_base = '0, cfg_completion_base = '0;
    logic [63:0] cfg_payload_low = PAY_LOW, cfg_payload_high = PAY_HIGH;
    logic [31:0] cfg_seed = '0, cfg_irq_delay = '0;
    logic [7:0] cfg_irq_batch = '0;

    logic host_valid = 1'b0;
    wire host_ready, host_ack, host_ok;
    logic [Q_W-1:0] host_queue = '0;
    logic [31:0] host_submit_tail = '0, host_complete_head = '0;
    logic [Q_W-1:0] inspect_queue = '0;
    wire inspect_enabled;
    wire [31:0] inspect_sq_head, inspect_sq_tail, inspect_cq_head, inspect_cq_tail;
    wire [7:0] inspect_irq_backlog;
    logic [QUEUES-1:0] irq_ack = '0;
    wire [QUEUES-1:0] irq;

    wire packet_valid, packet_first, packet_last, capture_valid, capture_abort, s_packet_ready;
    wire [63:0] packet_data, packet_cookie;
    wire [7:0] packet_keep;
    wire [15:0] packet_tag, capture_tag;
    wire [Q_W-1:0] packet_queue, capture_queue;
    wire [31:0] capture_length;
    wire packet_ready = 1'b1, capture_ready = 1'b1;
    wire s_packet_valid = 1'b0, s_packet_first = 1'b0, s_packet_last = 1'b0;
    wire [63:0] s_packet_data = '0;
    wire [7:0] s_packet_keep = '0;

    wire arvalid, arready, rvalid, rready, rlast;
    wire awvalid, awready, wvalid, wready, wlast, bvalid, bready;
    wire [63:0] araddr, rdata, awaddr, wdata;
    wire [7:0] arlen, awlen, wstrb;
    wire [2:0] arsize, awsize;
    wire [1:0] arburst, rresp, awburst, bresp;
    wire busy;
    wire [1:0] fatal_code;
    wire [63:0] cycles, pull_bytes, push_bytes;
    wire [31:0] completed, rejected;
    logic block_read_address = 1'b0;
    logic block_write_address = 1'b0;
    logic block_write_response = 1'b0;
    wire [63:0] inject_read_error = '1, inject_write_error = '1, inject_bad_last = '1;

    host_dma_queues #(
        .QUEUES(QUEUES), .RING_LOG(RING_LOG), .MAX_PACKET_BYTES(64), .MAX_BURST(MAX_BURST)
    ) engine (.*);
    tb_dma_memory #(.MAX_BURST(MAX_BURST), .SEED(32'h2317_9ec1)) memory (.*);

    // Logical counts start at zero in each generation. Only interface
    // projection wraps at 2**32 and only memory projection wraps at DEPTH.
    bit enabled [0:QUEUES-1];
    logic [31:0] seed [0:QUEUES-1];
    logic [63:0] submission [0:QUEUES-1], completion [0:QUEUES-1];
    integer produced [0:QUEUES-1], retired [0:QUEUES-1], reclaimed [0:QUEUES-1];
    integer batches [0:QUEUES-1], delays [0:QUEUES-1];
    integer irq_pending [0:QUEUES-1], irq_elapsed [0:QUEUES-1], waiting [0:QUEUES-1];
    bit irq_model [0:QUEUES-1];

    logic [15:0] fixture_tag [0:QUEUES-1][0:DEPTH-1];
    logic [63:0] fixture_cookie [0:QUEUES-1][0:DEPTH-1];
    bit fixture_force [0:QUEUES-1][0:DEPTH-1];
    integer fixture_sequence [0:QUEUES-1][0:DEPTH-1];
    integer fixture_counter;

    integer token, active;
    logic [63:0] expected_ar, expected_aw;
    logic [15:0] active_tag;
    logic [63:0] active_cookie;
    bit active_force;
    bit want_retire, saw_ar, saw_aw;
    integer read_beats, write_beats, expected_start;
    logic [63:0] prior_stamp;
    bit previous_cfg_ack, previous_cfg_ok, previous_host_ack, previous_host_ok;
    integer steps, started, total_done, accepted_bells, rejected_bells, accepted_cfg, rejected_cfg;
    integer full_credit_cycles, max_bypass, mod32_wraps, ring_wraps;
    integer irq_batch_edges, irq_timer_edges, irq_forced_edges, irq_ack_retire_edges;
    integer wait_b_cycles, rejected_reconfigure_busy, malformed_layouts, retired_by_queue [0:QUEUES-1];
    logic [31:0] rng = 32'h764c_e229;

    // Choose a repeatable pseudo-random integer below the given limit.
    function automatic integer draw(input integer limit);
        rng = rng ^ (rng << 13);
        rng = rng ^ (rng >> 17);
        rng = rng ^ (rng << 5);
        return int'(rng % $unsigned(limit));
    endfunction

    // Offset a queue's seeded absolute cursor, including across 32-bit wrap.
    function automatic logic [31:0] position(input integer queue_id, input integer offset);
        return seed[queue_id] + 32'(offset);
    endfunction

    // Map the key or absolute queue cursor into the appropriate table slot.
    function automatic integer index_of(input integer queue_id, input integer offset);
        return int'(position(queue_id, offset) & 32'(DEPTH-1));
    endfunction

    // Calculate the selected generation's SQ or CQ base address.
    function automatic logic [63:0] layout(input integer queue_id, input integer generation, input bit cq);
        return 64'(4096*(queue_id+1) + 256*(generation%3+1) + 1024*int'(cq));
    endfunction

    // Check that two half-open address ranges are disjoint.
    function automatic bit nonoverlap(
        input logic [63:0] first_start, first_size, second_start, second_size
    );
        return ({1'b0, first_start}+65'(first_size) <= {1'b0, second_start}) ||
               ({1'b0, second_start}+65'(second_size) <= {1'b0, first_start});
    endfunction

    // Check the proposed ring bases, size, aperture, and range ordering.
    function automatic bit geometry_good;
        logic [64:0] upper_sq, upper_cq;
        upper_sq = {1'b0,cfg_submission_base}+65'(DEPTH*32);
        upper_cq = {1'b0,cfg_completion_base}+65'(DEPTH*32);
        return cfg_submission_base % 32 == 0 && cfg_completion_base % 32 == 0 &&
            cfg_payload_low % 8 == 0 && cfg_payload_high % 8 == 0 && cfg_payload_low < cfg_payload_high &&
            upper_sq[64] == 0 && upper_cq[64] == 0 &&
            nonoverlap(cfg_submission_base, 64'(DEPTH*32), cfg_completion_base, 64'(DEPTH*32)) &&
            nonoverlap(cfg_submission_base, 64'(DEPTH*32), cfg_payload_low, cfg_payload_high-cfg_payload_low) &&
            nonoverlap(cfg_completion_base, 64'(DEPTH*32), cfg_payload_low, cfg_payload_high-cfg_payload_low);
    endfunction

    // Report whether this queue has work and room for its completion.
    function automatic bit queue_eligible(input integer queue_id);
        return enabled[queue_id] && produced[queue_id] != retired[queue_id] &&
            retired[queue_id]-reclaimed[queue_id] < DEPTH;
    endfunction

    // Reset independently tracked queue, scheduler, and interrupt state.
    task automatic reset_oracle;
        token = 0;
        active = -1;
        total_done = 0;
        started = 0;
        want_retire = 1'b0;
        saw_ar = 1'b0;
        saw_aw = 1'b0;
        read_beats = 0;
        write_beats = 0;
        expected_start = 0;
        prior_stamp = '0;
        previous_cfg_ack = 1'b0;
        previous_cfg_ok = 1'b0;
        previous_host_ack = 1'b0;
        previous_host_ok = 1'b0;
        for (integer q = 0; q < QUEUES; q++) begin
            enabled[q] = 1'b0;
            seed[q] = '0;
            submission[q] = '0;
            completion[q] = '0;
            produced[q] = 0;
            retired[q] = 0;
            reclaimed[q] = 0;
            batches[q] = 0;
            delays[q] = 0;
            waiting[q] = 0;
            irq_pending[q] = 0;
            irq_elapsed[q] = 0;
            irq_model[q] = 1'b0;
            retired_by_queue[q] = 0;
            for (integer s = 0; s < DEPTH; s++)
                fixture_sequence[q][s] = -1;
        end
    endtask

    // Compare exposed queue state and counters with the scoreboard.
    task automatic inspect_and_check;
        integer q;
        q = int'(inspect_queue);
        if ({cfg_ack,cfg_ok,host_ack,host_ok} !==
            {previous_cfg_ack,previous_cfg_ok,previous_host_ack,previous_host_ok})
            $fatal(1, "management response disagrees with host ownership, cycle %0d", steps);
        if (fatal_code !== 0 || rejected !== 0 || pull_bytes !== 0 || push_bytes !== 0 ||
            packet_valid || capture_valid || capture_abort || s_packet_ready)
            $fatal(1, "FENCE traffic activated a payload or fault path");
        if (busy !== (active >= 0) || completed !== 32'(total_done))
            $fatal(1, "retirement disagrees with B-response boundary at cycle %0d", steps);
        for (integer b = 0; b < QUEUES; b++) begin
            if (irq[b] !== irq_model[b])
                $fatal(1, "IRQ bank %0d at cycle %0d expected %b got %b", b, steps, irq_model[b], irq[b]);
            if (produced[b] < retired[b] || retired[b] < reclaimed[b] ||
                produced[b]-retired[b] > DEPTH || retired[b]-reclaimed[b] > DEPTH)
                $fatal(1, "model lost a ring credit for queue %0d", b);
            if (enabled[b] && produced[b] > retired[b] && retired[b]-reclaimed[b] == DEPTH)
                full_credit_cycles++;
        end
        if (q >= QUEUES || !enabled[q]) begin
            if ({inspect_enabled,inspect_sq_head,inspect_sq_tail,inspect_cq_head,inspect_cq_tail,
                inspect_irq_backlog} !== '0)
                $fatal(1, "disabled/invalid inspect returned stale metadata");
        end else begin
            if (!inspect_enabled || inspect_sq_head !== position(q,retired[q]) ||
                inspect_sq_tail !== position(q,produced[q]) ||
                inspect_cq_head !== position(q,reclaimed[q]) ||
                inspect_cq_tail !== position(q,retired[q]) || inspect_irq_backlog !== 8'(irq_pending[q]))
                $fatal(1, "queue %0d unexpected inspection at cycle %0d", q, steps);
        end
    endtask

    // Predict which eligible queue should win the next scheduler grant.
    task automatic choose_work;
        integer winner, candidate, slot;
        winner = -1;
        for (integer offset = 0; offset < QUEUES; offset++) begin
            candidate = (token + offset) % QUEUES;
            if (winner < 0 && queue_eligible(candidate))
                winner = candidate;
        end
        for (integer q = 0; q < QUEUES; q++) begin
            if (!queue_eligible(q) || q == winner)
                waiting[q] = 0;
            else if (winner >= 0) begin
                waiting[q]++;
                if (waiting[q] > max_bypass)
                    max_bypass = waiting[q];
                if (waiting[q] >= QUEUES)
                    $fatal(1, "queue %0d skipped by too many grants", q);
            end
        end
        if (winner >= 0) begin
            active = winner;
            expected_start = steps;
            start_descriptor(winner);
            token = (winner+1) % QUEUES;
            slot = index_of(winner, retired[winner]);
            if (fixture_sequence[winner][slot] != retired[winner])
                $fatal(1, "host fixture ownership differs from published submission slot");
            active_tag = fixture_tag[winner][slot];
            active_cookie = fixture_cookie[winner][slot];
            active_force = fixture_force[winner][slot];
        end
    endtask

    // Record the selected descriptor and its expected AXI addresses.
    task automatic start_descriptor(input integer winner);
        expected_ar = submission[winner] + 64'(32*index_of(winner,retired[winner]));
        expected_aw = completion[winner] + 64'(32*index_of(winner,retired[winner]));
        saw_ar = 1'b0;
        saw_aw = 1'b0;
        read_beats = 0;
        write_beats = 0;
        started++;
    endtask

    // Check every memory handshake against the expected descriptor or CQ access.
    task automatic validate_axi;
        logic [63:0] wanted;
        if (!busy && (arvalid || awvalid || wvalid || bready || rready))
            $fatal(1, "memory traffic generated outside a grant");
        if (arvalid && arready) begin
            if (active < 0 || saw_ar || saw_aw || araddr !== expected_ar || arlen !== 3)
                $fatal(1, "SQ fetch mismatches scheduled queue: got %h expected %h", araddr, expected_ar);
            saw_ar = 1'b1;
        end
        if (rvalid && rready) begin
            if (!saw_ar || saw_aw || read_beats >= 4 || rresp !== 0 || rlast !== (read_beats == 3))
                $fatal(1, "invalid FENCE read beat");
            case (read_beats)
                0: wanted = {`DMA_FENCE,7'd0,active_force,active_tag,32'd0};
                2: wanted = active_cookie;
                default: wanted = '0;
            endcase
            if (rdata !== wanted)
                $fatal(1, "SQ data differs from posted descriptor fixture");
            read_beats++;
        end
        if (awvalid && awready) begin
            if (active < 0 || !saw_ar || saw_aw || read_beats != 4 || awaddr !== expected_aw || awlen !== 3)
                $fatal(1, "CQ write mismatches reserved credit: got %h expected %h", awaddr, expected_aw);
            saw_aw = 1'b1;
        end
        if (wvalid && wready) begin
            if (!saw_aw || write_beats >= 4 || wstrb !== 8'hff || wlast !== (write_beats == 3))
                $fatal(1, "malformed FENCE completion transaction");
            case (write_beats)
                0: wanted = {`DMA_OK,`DMA_FENCE,active_tag,32'd0};
                1: wanted = active_cookie;
                2: wanted = {32'd0,8'(active),24'd0};
                default: begin
                    wanted = wdata;
                    if (wdata < 64'(expected_start) || wdata <= prior_stamp || wdata >= cycles)
                        $fatal(1, "FENCE completion timestamp outside execution order");
                    prior_stamp = wdata;
                end
            endcase
            if (wdata !== wanted)
                $fatal(1, "CQ beat %0d does not match descriptor", write_beats);
            write_beats++;
        end
        if (block_write_response && saw_aw && write_beats == 4 && !want_retire)
            wait_b_cycles++;
        if (bvalid && bready) begin
            if (!saw_aw || write_beats != 4 || bresp !== 0 || want_retire)
                $fatal(1, "unexpected B handshake");
        end
    endtask

    // Advance the predicted absolute cursors only after completion writeback.
    task automatic retire_descriptor;
        integer slot;
        slot = index_of(active, retired[active]);
        if (memory.word(expected_aw) !== {`DMA_OK,`DMA_FENCE,active_tag,32'd0} ||
            memory.word(expected_aw+8) !== active_cookie ||
            memory.word(expected_aw+16) !== {32'd0,8'(active),24'd0} ||
            memory.word(expected_aw+24) !== prior_stamp)
            $fatal(1, "completion was not written before retirement");
        if (position(active,retired[active]) == 32'hffff_ffff)
            mod32_wraps++;
        if (slot == DEPTH-1)
            ring_wraps++;
        retired[active]++;
        retired_by_queue[active]++;
        total_done++;
        active = -1;
    endtask

    // Model one queue's IRQ timer, acknowledgment and optional retirement.
    task automatic advance_interrupt(input integer q, input bit did_retire, input bit reconfigured);
        bit force_tick, reaches_batch, reaches_timer, old_irq;
        old_irq = irq_model[q];
        if (reconfigured) begin
            irq_pending[q] = 0;
            irq_elapsed[q] = 0;
            irq_model[q] = 1'b0;
        end else begin
            if (irq_ack[q]) begin
                irq_pending[q] = 0;
                irq_elapsed[q] = 0;
                irq_model[q] = 1'b0;
                if (did_retire)
                    irq_ack_retire_edges++;
            end
            if (did_retire && irq_pending[q] < 255)
                irq_pending[q]++;
            if (irq_pending[q] != 0)
                irq_elapsed[q]++;
            force_tick = did_retire && active_force;
            reaches_batch = irq_pending[q] != 0 && batches[q] != 0 && irq_pending[q] >= batches[q];
            reaches_timer = irq_pending[q] != 0 && delays[q] != 0 && irq_elapsed[q] >= delays[q];
            irq_model[q] = irq_model[q] || force_tick || reaches_batch || reaches_timer;
            if (!old_irq && irq_model[q]) begin
                if (force_tick) irq_forced_edges++;
                else if (reaches_batch) irq_batch_edges++;
                else if (reaches_timer) irq_timer_edges++;
            end
        end
    endtask

    // Check accepted or rejected doorbells and update the independent queue model.
    task automatic check_host_transfer;
        integer q, posted, consumed;
        q = int'(host_queue);
        previous_host_ack = host_valid && host_ready;
        previous_host_ok = 1'b0;
        if (previous_host_ack) begin
            posted = -1;
            consumed = -1;
            if (q < QUEUES && enabled[q]) begin
                // Enumerate only mathematically legal forward positions,
                // including wrap, independent of the hardware's subtraction.
                for (integer n = produced[q]; n <= retired[q]+DEPTH; n++)
                    if (position(q,n) == host_submit_tail) posted = n;
                for (integer n = reclaimed[q]; n <= retired[q]; n++)
                    if (position(q,n) == host_complete_head) consumed = n;
            end
            if (posted >= 0 && consumed >= 0) begin
                for (integer n = produced[q]; n < posted; n++)
                    if (fixture_sequence[q][index_of(q,n)] != n)
                        $fatal(1, "host published an unwritten fixture");
                produced[q] = posted;
                reclaimed[q] = consumed;
                previous_host_ok = 1'b1;
                accepted_bells++;
            end else begin
                rejected_bells++;
            end
        end
    endtask

    // Check configuration validity and record the queue that was reconfigured.
    task automatic check_config_transfer(output integer reconfigured);
        integer q;
        q = int'(cfg_queue);
        reconfigured = -1;
        previous_cfg_ack = cfg_valid && cfg_ready;
        previous_cfg_ok = 1'b0;
        if (previous_cfg_ack) begin
            if (q < QUEUES && (!enabled[q] || (produced[q] == retired[q] && reclaimed[q] == retired[q])) &&
                (!cfg_enable || geometry_good())) begin
                enabled[q] = cfg_enable;
                seed[q] = cfg_seed;
                submission[q] = cfg_submission_base;
                completion[q] = cfg_completion_base;
                retired[q] = 0;
                reclaimed[q] = 0;
                produced[q] = 0;
                batches[q] = int'(cfg_irq_batch);
                delays[q] = int'(cfg_irq_delay);
                reconfigured = q;
                previous_cfg_ok = 1'b1;
                accepted_cfg++;
                for (integer slot = 0; slot < DEPTH; slot++)
                    fixture_sequence[q][slot] = -1;
            end else begin
                if (q < QUEUES && enabled[q] && (produced[q] != retired[q] || reclaimed[q] != retired[q]))
                    rejected_reconfigure_busy++;
                if (cfg_enable && !geometry_good())
                    malformed_layouts++;
                rejected_cfg++;
            end
        end
    endtask

    always @(posedge clk) begin : audit_cycle
        integer changed;
        if (reset) begin
            reset_oracle();
        end else begin
            steps++;
            if (steps > LIMIT)
                $fatal(1, "DMA contention simulation timeout");
            inspect_and_check();
            if (cfg_ready !== !busy || host_ready !== !(cfg_valid && !busy))
                $fatal(1, "control acceptance priority changed");
            validate_axi();
            if (!busy && !cfg_valid)
                choose_work();
            check_host_transfer();
            check_config_transfer(changed);
            for (integer q = 0; q < QUEUES; q++)
                advance_interrupt(q,want_retire && active == q,changed == q);
            if (want_retire)
                retire_descriptor();
            want_retire = bvalid && bready;
        end
    end

    always @(negedge clk) begin
        if (!reset)
            inspect_queue <= Q_W'(steps % (QUEUES+1));
    end

    // Present one complete config transaction and wait for its acknowledgment.
    task automatic configure_queue(
        input integer q, input bit enable, input integer generation, input logic [31:0] start,
        input integer batch, input integer delay_cycles, input integer malformed, input bit expect_ok
    );
        @(negedge clk);
        cfg_queue = Q_W'(q);
        cfg_enable = enable;
        cfg_submission_base = layout(q,generation,0);
        cfg_completion_base = layout(q,generation,1);
        cfg_payload_low = PAY_LOW;
        cfg_payload_high = PAY_HIGH;
        cfg_seed = start;
        cfg_irq_batch = 8'(batch);
        cfg_irq_delay = 32'(delay_cycles);
        case (malformed)
            1: cfg_submission_base++;
            2: cfg_completion_base = cfg_submission_base;
            3: cfg_payload_high = cfg_payload_low;
            4: cfg_completion_base = 64'hffff_ffff_ffff_ffe0;
            default: ;
        endcase
        cfg_valid = 1'b1;
        @(posedge clk);
        while (!cfg_ready) @(posedge clk);
        #1;
        if (!cfg_ack || cfg_ok !== expect_ok)
            $fatal(1, "configuration command q=%0d enable=%b expected=%b got=%b",
                q, enable, expect_ok, cfg_ok);
        @(negedge clk);
        cfg_valid = 1'b0;
    endtask

    // Drive one doorbell request with absolute submit and release cursors.
    task automatic bell(
        input integer q, input logic [31:0] sq, cq, input bit expect_ok
    );
        @(negedge clk);
        host_queue = Q_W'(q);
        host_submit_tail = sq;
        host_complete_head = cq;
        host_valid = 1'b1;
        @(posedge clk);
        while (!host_ready) @(posedge clk);
        #1;
        if (!host_ack || host_ok !== expect_ok)
            $fatal(1, "doorbell q=%0d SQ=%h CQ=%h expected=%b",q,sq,cq,expect_ok);
        @(negedge clk);
        host_valid = 1'b0;
    endtask

    // Initialize memory with complete FENCE descriptors for a chosen queue.
    task automatic create_fences(
        input integer q, input integer entries, input bit immediate_first
    );
        integer slot, logical_seq;
        logic [63:0] address;
        if (!enabled[q] || entries < 0 || produced[q]-retired[q]+entries > DEPTH)
            $fatal(1, "test tried to overwrite a live host SQ slot");
        for (integer n = 0; n < entries; n++) begin
            logical_seq = produced[q]+n;
            slot = index_of(q,logical_seq);
            address = submission[q] + 64'(32*slot);
            fixture_counter++;
            fixture_tag[q][slot] = 16'(fixture_counter*31+q);
            fixture_cookie[q][slot] = 64'h8db9_2346_f0_70_0000 ^ 64'(fixture_counter*104729+q);
            fixture_force[q][slot] = immediate_first && n == 0;
            fixture_sequence[q][slot] = logical_seq;
            memory.store_word(address,{
                `DMA_FENCE,7'd0,fixture_force[q][slot],fixture_tag[q][slot],32'd0
            });
            memory.store_word(address+8,64'd0);
            memory.store_word(address+16,fixture_cookie[q][slot]);
            memory.store_word(address+24,64'd0);
        end
    endtask

    // Stage additional FENCE descriptors and ring the submit doorbell.
    task automatic add_work(input integer q, input integer entries, input bit force_first);
        @(negedge clk);
        create_fences(q,entries,force_first);
        bell(q,position(q,produced[q]+entries),position(q,reclaimed[q]),1'b1);
    endtask

    // Release completed entries by moving the model CQ head through a doorbell.
    task automatic reap(input integer q, input integer count);
        bell(q,position(q,produced[q]),position(q,reclaimed[q]+count),1'b1);
    endtask

    // Wait for the specified retired count and fail on a stalled engine.
    task automatic wait_retired(input integer target);
        integer prior;
        prior = steps;
        while (completed < 32'(target) && steps-prior < 1000)
            @(negedge clk);
        if (completed !== 32'(target))
            $fatal(1,"expected exactly %0d cumulative retirements, found %0d",target,completed);
    endtask

    // Acknowledge a single queue interrupt without changing CQ ownership.
    task automatic ack_one(input integer q);
        @(negedge clk);
        irq_ack[q] = 1'b1;
        @(negedge clk);
        irq_ack[q] = 1'b0;
    endtask

    bit automatic_irqs = 1'b0;
    bit automatic_bus = 1'b0;
    logic [31:0] pacing = 32'h3751_429c;
    always @(negedge clk) begin
        if (!reset && automatic_irqs) begin
            irq_ack <= '0;
            if (want_retire && started % 3 == 0 && active >= 0)
                irq_ack[active] <= 1'b1;
            else if (steps % 9 == 0)
                irq_ack[(steps/9)%QUEUES] <= 1'b1;
        end
        if (!reset && automatic_bus) begin
            pacing <= {pacing[30:0],pacing[31]^pacing[21]^pacing[1]^pacing[0]};
            block_read_address <= (steps % 159 < 21) || pacing[4:0] == 0;
            block_write_address <= (steps % 173 < 29) || pacing[9:5] == 0;
            block_write_response <= (steps % 79 < 25) || pacing[13:10] == 0;
        end
    end

    // Fill a CQ deliberately and observe credit backpressure.
    task automatic direct_completion_credit;
        block_read_address = 1'b1;
        block_write_response = 1'b1;
        for (integer q = 0; q < QUEUES; q++)
            add_work(q,DEPTH,q==2);

        bell(0,position(0,produced[0]+1),position(0,reclaimed[0]),1'b0);
        bell(1,position(1,produced[1]-1),position(1,reclaimed[1]),1'b0);
        block_read_address = 1'b0;
        wait (memory.w_count == 4);
        repeat (12) @(negedge clk);
        if (completed != 0 || irq != 0 || !busy)
            $fatal(1,"completion became visible before B response");
        bell(0,position(0,produced[0]),position(0,reclaimed[0]+1),1'b0);
        block_write_response = 1'b0;
        wait_retired(QUEUES*DEPTH);
        repeat (10) @(negedge clk);
        if (irq !== '1)
            $fatal(1, "batch/timer/forced IRQ did not all assert");
        for (integer q = 0; q < QUEUES; q++)
            ack_one(q);

        for (integer q = 0; q < QUEUES; q++)
            add_work(q,DEPTH,q==2);
        check_blocked();
        configure_queue(0,1'b0,1,32'd10,0,0,0,1'b0);
        reap(1,1);
        wait_retired(QUEUES*DEPTH+1);
        if (retired_by_queue[0] != DEPTH || retired_by_queue[1] != DEPTH+1 ||
            retired_by_queue[2] != DEPTH)
            $fatal(1, "returning credit to one bank moved a different bank");
        check_blocked();
        for (integer q = 0; q < QUEUES; q++)
            reap(q,retired[q]-reclaimed[q]);
        wait_retired(2*QUEUES*DEPTH);
        configure_queue(2,1'b1,2,32'd40,0,0,0,1'b0);
        for (integer q = 0; q < QUEUES; q++)
            reap(q,retired[q]-reclaimed[q]);
    endtask

    // Ensure a selected hold condition prevents premature progress.
    task automatic check_blocked;
        integer prior, receipts;
        prior = int'(completed);
        receipts = memory.ar_count;
        repeat (34) begin
            @(negedge clk);
            if (completed !== 32'(prior) || memory.ar_count != receipts || busy)
                $fatal(1, "full completion ring leaked a DMA transaction");
        end
    endtask

    // Check that a simultaneous reconfiguration takes priority over a doorbell.
    task automatic config_doorbell_priority;
        // The old no-op doorbell is legal before a rebase. It must wait
        // through the configuration edge and then be checked in the new
        // generation, where those absolute positions are stale.
        @(negedge clk);
        if (busy || produced[1] != retired[1] || retired[1] != reclaimed[1])
            $fatal(1, "test did not establish an empty queue before rebasing");
        cfg_queue = Q_W'(1);
        cfg_enable = 1'b1;
        cfg_submission_base = layout(1,1,0);
        cfg_completion_base = layout(1,1,1);
        cfg_payload_low = PAY_LOW;
        cfg_payload_high = PAY_HIGH;
        cfg_seed = 32'h0000_2200;
        cfg_irq_batch = 0;
        cfg_irq_delay = 10;
        cfg_valid = 1'b1;
        host_queue = Q_W'(1);
        host_submit_tail = position(1,produced[1]);
        host_complete_head = position(1,reclaimed[1]);
        host_valid = 1'b1;
        @(posedge clk);
        if (!cfg_ready || host_ready)
            $fatal(1, "configuration did not take precedence over an idle doorbell");
        #1;
        if (!cfg_ack || !cfg_ok || host_ack)
            $fatal(1, "blocked host transport acknowledged during reconfiguration");
        @(negedge clk);
        cfg_valid = 1'b0;
        @(posedge clk);
        #1;
        if (!host_ack || host_ok)
            $fatal(1, "stale doorbell was accepted into the new generation");
        @(negedge clk);
        host_valid = 1'b0;
        bell(1,position(1,0),position(1,0),1'b1);
    endtask

    // Run directed configuration and queue wraparound cases.
    task automatic management_cases;
        for (integer failure = 1; failure <= 4; failure++)
            configure_queue(0,1'b1,1,32'd100,3,0,failure,1'b0);
        configure_queue(QUEUES,1'b1,1,32'd100,3,0,0,1'b0);
        bell(QUEUES,32'd0,32'd0,1'b0);
        configure_queue(0,1'b0,1,32'hffff_ffff,0,0,1,1'b1);
        bell(0,32'hffff_ffff,32'hffff_ffff,1'b0);
        configure_queue(0,1'b1,2,32'hffff_fffc,3,0,0,1'b1);
        add_work(0,1,1'b0);
        wait_retired(2*QUEUES*DEPTH+1);
        create_fences(0,1,1'b0);
        // A valid new SQ entry paired with an invalid CQ reclamation is
        // rejected as a transaction. It can be published on a later bell.
        bell(0,position(0,produced[0]+1),position(0,retired[0]+1),1'b0);
        bell(0,position(0,produced[0]+1),position(0,reclaimed[0]),1'b1);
        wait_retired(2*QUEUES*DEPTH+2);
        reap(0,2);
        configure_queue(0,1'b1,1,32'hffff_fffd,3,0,0,1'b1);
    endtask

    // Drive repeatable mixed queue and host traffic for the requested run length.
    task automatic run_random(input integer iterations);
        integer q, action, credit, available, amount;
        automatic_irqs = 1'b1;
        automatic_bus = 1'b1;
        for (integer step = 0; step < iterations; step++) begin
            q = draw(QUEUES);
            action = draw(12);
            credit = retired[q]-reclaimed[q];
            available = DEPTH+retired[q]-produced[q];
            if (action <= 4 && available > 0) begin
                amount = 1+draw(available);
                add_work(q,amount,draw(4)==0);
            end else if (action <= 7 && credit > 0) begin
                reap(q,1+draw(credit));
            end else if (action == 8) begin
                // A full ring's worth plus an extra slot is too large even
                // when this queue happens to retire during the host operation.
                bell(q,position(q,produced[q]+DEPTH+1),position(q,reclaimed[q]),1'b0);
            end else if (action == 9) begin
                bell(q,position(q,produced[q]-1),position(q,reclaimed[q]),1'b0);
            end else if (action == 10 && produced[q] == retired[q] && reclaimed[q] == retired[q]) begin
                configure_queue(q,1'b1,1+draw(3),32'hffff_fffc+32'(draw(4)),
                       draw(5),3+draw(19),0,1'b1);
            end else begin
                repeat (1+draw(7)) @(negedge clk);
            end
        end
        @(negedge clk);
        automatic_bus = 1'b0;
        automatic_irqs = 1'b0;
        @(negedge clk);
        block_read_address = 1'b0;
        block_write_address = 1'b0;
        block_write_response = 1'b0;
        irq_ack = '0;
        drain_all();
    endtask

    // Release blocked credits and prove all expected work eventually retires.
    task automatic drain_all;
        integer start_cycle;
        bit remaining;
        start_cycle = steps;
        do begin
            remaining = 1'b0;
            for (integer q = 0; q < QUEUES; q++) begin
                if (retired[q] > reclaimed[q])
                    reap(q,retired[q]-reclaimed[q]);
                remaining |= (retired[q] != produced[q] || retired[q] != reclaimed[q]);
            end
            repeat (3) @(negedge clk);
            if (steps-start_cycle > 1000)
                $fatal(1, "credit drain failed to converge");
        end while (remaining || busy);
        for (integer q = 0; q < QUEUES; q++)
            ack_one(q);
    endtask

    initial begin : scenario
        fixture_counter = 0;
        steps = 0;
        accepted_bells = 0;
        rejected_bells = 0;
        accepted_cfg = 0;
        rejected_cfg = 0;
        full_credit_cycles = 0;
        max_bypass = 0;
        mod32_wraps = 0;
        ring_wraps = 0;
        irq_batch_edges = 0;
        irq_timer_edges = 0;
        irq_forced_edges = 0;
        irq_ack_retire_edges = 0;
        wait_b_cycles = 0;
        rejected_reconfigure_busy = 0;
        malformed_layouts = 0;

        memory.clear_memory(8'h91);
        repeat (3) @(negedge clk);
        reset = 1'b0;
        configure_queue(0,1'b1,0,32'hffff_fffe,3,0,0,1'b1);
        configure_queue(1,1'b1,0,32'hffff_ffff,0,10,0,1'b1);
        configure_queue(2,1'b1,0,32'h0000_0003,0,0,0,1'b1);
        direct_completion_credit();
        management_cases();
        config_doorbell_priority();
        run_random(940);
        if (total_done < 70 || started != total_done || memory.ar_count != total_done ||
            memory.aw_count != total_done || memory.b_count != total_done ||
            memory.r_count != total_done*4 || memory.w_count != total_done*4)
            $fatal(1,"expected exact AXI burst counts for %0d FENCE transactions",total_done);
        for (integer q = 0; q < QUEUES; q++) begin
            if (retired_by_queue[q] < 10 || produced[q] != retired[q] ||
                retired[q] != reclaimed[q] || irq[q])
                $fatal(1,"queue %0d did not complete or drain",q);
        end
        if (max_bypass != QUEUES-1 || mod32_wraps < 3 || ring_wraps < 30 ||
            full_credit_cycles < 250 || accepted_bells < 25 || rejected_bells < 25 ||
            accepted_cfg < 6 || rejected_cfg < 6 || rejected_reconfigure_busy < 2 ||
            malformed_layouts < 4 || wait_b_cycles < 15 || irq_batch_edges == 0 ||
            irq_timer_edges == 0 || irq_forced_edges == 0 || irq_ack_retire_edges == 0)
            $fatal(1,"missing contention coverage wraps=%0d/%0d blocked=%0d max_bypass=%0d cfg=%0d/%0d ack+retire=%0d",
                mod32_wraps,ring_wraps,full_credit_cycles,max_bypass,accepted_cfg,rejected_cfg,irq_ack_retire_edges);
        $display("PASS DMA contention FENCEs=%0d q=%0d/%0d/%0d 32b_wrap=%0d ring_wrap=%0d credit_block_cycles=%0d max_bypass=%0d rejected_bells=%0d B_hold=%0d IRQ=%0d/%0d/%0d ack_retire=%0d",
            total_done,retired_by_queue[0],retired_by_queue[1],retired_by_queue[2],
            mod32_wraps,ring_wraps,full_credit_cycles,max_bypass,rejected_bells,
            wait_b_cycles,irq_batch_edges,irq_timer_edges,irq_forced_edges,irq_ack_retire_edges);
        $finish;
    end
endmodule

`default_nettype wire
