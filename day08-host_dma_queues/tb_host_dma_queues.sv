`timescale 1ns/1ps

module tb_host_dma_queues #(
    parameter bit ENFORCE_QUEUE_ISOLATION = 1'b0,
    parameter integer TEST_SEGMENTS = 8
);
    localparam integer MAX_BURST = 8;
    localparam integer RING_LOG = 3;
    localparam integer SLOTS = 1 << RING_LOG;
    localparam integer MAX_PACKET = 512;
    logic clk = 0;
    always #5 clk = !clk;
    logic reset = 1;

    logic cfg_valid = 0, cfg_queue = 0, cfg_enable = 0;
    wire cfg_ready, cfg_ack, cfg_ok;
    logic [63:0] cfg_submission_base = 0, cfg_completion_base = 0;
    logic [63:0] cfg_payload_low = 0, cfg_payload_high = 0;
    logic [31:0] cfg_seed = 0, cfg_irq_delay = 0;
    logic [7:0] cfg_irq_batch = 0;
    logic host_valid = 0, host_queue = 0;
    wire host_ready, host_ack, host_ok;
    logic [31:0] host_submit_tail = 0, host_complete_head = 0;
    logic inspect_queue = 0;
    wire inspect_enabled;
    wire [31:0] inspect_sq_head, inspect_sq_tail, inspect_cq_head, inspect_cq_tail;
    wire [7:0] inspect_irq_backlog;
    logic [1:0] irq_ack = 0;
    wire [1:0] irq;
    wire packet_valid;
    logic packet_ready = 0;
    wire [63:0] packet_data;
    wire [7:0] packet_keep;
    wire packet_first, packet_last;
    wire [15:0] packet_tag;
    wire packet_queue;
    wire [63:0] packet_cookie;

    wire capture_valid;
    logic capture_ready = 0;
    wire [15:0] capture_tag;
    wire capture_queue;
    wire [31:0] capture_length;
    wire capture_abort;
    logic s_packet_valid = 0;
    wire s_packet_ready;
    logic [63:0] s_packet_data = 0;
    logic [7:0] s_packet_keep = 0;
    logic s_packet_first = 0, s_packet_last = 0;

    wire arvalid, arready, rvalid, rready, rlast;
    wire [63:0] araddr, rdata, awaddr, wdata;
    wire [7:0] arlen, awlen, wstrb;
    wire [2:0] arsize, awsize;
    wire [1:0] arburst, awburst, rresp, bresp;
    wire awvalid, awready, wvalid, wready, wlast, bvalid, bready;
    wire busy;
    wire [1:0] fatal_code;
    wire [63:0] cycles, pull_bytes, push_bytes;
    wire [31:0] completed, rejected;

    logic block_read_address = 0, block_write_address = 0, block_write_response = 0;
    logic [63:0] inject_read_error = '1, inject_write_error = '1, inject_bad_last = '1;

    host_dma_queues #(
        .QUEUES(2), .RING_LOG(RING_LOG), .MAX_PACKET_BYTES(MAX_PACKET),
        .MAX_BURST(MAX_BURST), .CAPTURE_TIMEOUT(48),
        .ENFORCE_QUEUE_ISOLATION(ENFORCE_QUEUE_ISOLATION), .MAX_SEGMENTS(TEST_SEGMENTS)
    ) dut (.*);
    tb_dma_memory #(.MAX_BURST(MAX_BURST), .SEED(32'hbcf1ab45)) memory (.*);

    logic [31:0] tail [0:1], host_cq [0:1], consumed [0:1];
    logic [63:0] last_completion [0:1];
    int compared = 0, processed_errors = 0;
    int cfg_requests = 0, host_requests = 0;
    int transferred_packets = 0, outgoing_bytes = 0, aborted_captures = 0;
    int packet_stalls = 0, capture_stalls = 0;
    int expected_packet_bytes = 0, expected_seed = 0, packet_offset = 0;
    logic [15:0] expected_packet_tag = 0;
    logic [63:0] expected_cookie = 0;
    logic expected_queue = 0, enable_tx_sink = 0, allow_tx = 0;
    logic tx_held = 0, cap_held = 0;
    logic [154:0] held_packet;
    logic [48:0] held_capture;
    logic [31:0] sink_bits = 32'ha57b1273;
    integer completion_capture = 0, packet_capture = 0;
    logic integrity_run = 0;

    // Open optional completion and packet logs when a capture path is supplied.
    task automatic open_capture;
        string directory;
        begin
            if ($value$plusargs("capture=%s",directory)) begin
                completion_capture = $fopen({directory,"/completions.hex"},"w");
                packet_capture = $fopen({directory,"/packets.txt"},"w");
                if (!completion_capture || !packet_capture)
                    $fatal(1,"Could not open DMA capture files");
            end
        end
    endtask

    // Emit the exact 32 CQ bytes from simulated host memory.
    task automatic save_completion(input logic [63:0] location);
        begin
            if (completion_capture) begin
                for (integer lane = 0; lane < 32; lane++)
                    $fwrite(completion_capture,"%02h",memory.memory[int'(location)+lane]);
                $fdisplay(completion_capture,"");
            end
        end
    endtask

    // Select the configured submission-ring address for this fixture queue.
    function automatic logic [63:0] sq_base(input logic q);
        sq_base = q ? 64'h900 : 64'h100;
    endfunction
    // Select the configured completion-ring address for this fixture queue.
    function automatic logic [63:0] cq_base(input logic q);
        cq_base = q ? 64'hc00 : 64'h300;
    endfunction
    // Return the first DMA data address permitted for this queue.
    function automatic logic [63:0] aperture_start(input logic q);
        aperture_start = q ? 64'h5000 : 64'hf00;
    endfunction
    // Return the exclusive upper data address for this queue.
    function automatic logic [63:0] aperture_end(input logic q);
        aperture_end = q ? 64'h8000 : 64'h4800;
    endfunction
    // Map an absolute 32-bit ring cursor to its wrapped physical slot.
    function automatic logic [63:0] slot(input logic [63:0] base, input logic [31:0] sequence_number);
        slot = base + (64'(sequence_number & (SLOTS-1)) << 5);
    endfunction
    // Generate the expected fixture byte from its offset and a seed.
    function automatic logic [7:0] pattern(input integer seed, input integer offset);
        logic [31:0] x;
        begin
            x = (32'(offset) * 32'h70fcab93) + 32'(seed);
            x = (x ^ (x >> 17)) + ((x >> 7) ^ 32'hbe6701ae);
            pattern = 8'(x);
        end
    endfunction

    // Calculate the fixture's CRC independently of the hardware datapath.
    function automatic logic [31:0] reference_crc(input integer seed, input integer bytes);
        logic [31:0] crc;
        logic [31:0] reduction [0:255];
        logic [31:0] entry;
        begin
            for (integer i = 0; i < 256; i++) begin
                entry = 32'(i);
                repeat (8)
                    entry = (entry >> 1) ^ (entry[0] ? 32'hedb88320 : 0);
                reduction[i] = entry;
            end
            crc = '1;
            for (integer i = 0; i < bytes; i++)
                crc = reduction[8'(crc) ^ pattern(seed,i)] ^ (crc >> 8);
            reference_crc = ~crc;
        end
    endfunction

    always @(negedge clk) begin
        sink_bits = {sink_bits[30:0],sink_bits[31]^sink_bits[21]^sink_bits[1]^sink_bits[0]};
        packet_ready = enable_tx_sink && sink_bits[3:0] > 5;
    end

    always @(posedge clk) begin
        if (reset) begin
            tx_held = 0;
            cap_held = 0;
        end else begin
            if (integrity_run && packet_valid)
                $fatal(1,"An integrity request leaked a packet to the PULL stream");
            if (tx_held && (!packet_valid || {packet_data,packet_keep,packet_first,packet_last,
                                             packet_tag,packet_queue,packet_cookie} !== held_packet))
                $fatal(1,"TX sidebands changed under backpressure");
            if (cap_held && (!capture_valid || {capture_tag,capture_queue,capture_length} !== held_capture))
                $fatal(1,"Capture notice changed under backpressure");
            tx_held = packet_valid && !packet_ready;
            cap_held = capture_valid && !capture_ready;
            if (tx_held) begin
                held_packet = {packet_data,packet_keep,packet_first,packet_last,
                               packet_tag,packet_queue,packet_cookie};
                packet_stalls++;
            end
            if (cap_held) begin
                held_capture = {capture_tag,capture_queue,capture_length};
                capture_stalls++;
            end
            if (capture_abort) aborted_captures++;
            if (packet_valid && packet_ready) begin
                if (packet_capture)
                    $fdisplay(packet_capture,"%0d 0x%h 0x%h 0x%h 0x%h %0d %0d",
                              packet_queue,packet_tag,packet_cookie,packet_data,packet_keep,
                              packet_first,packet_last);
                if (!allow_tx || packet_tag != expected_packet_tag ||
                    packet_queue != expected_queue || packet_cookie != expected_cookie)
                    $fatal(1,"Unexpected packet or incorrect request identity");
                if (packet_first != (packet_offset == 0) ||
                    packet_last != (packet_offset + 8 >= expected_packet_bytes))
                    $fatal(1,"Packet boundary mismatch at byte %0d",packet_offset);
                for (integer lane = 0; lane < 8; lane++) begin
                    if (packet_offset+lane < expected_packet_bytes) begin
                        if (!packet_keep[lane] ||
                            packet_data[8*lane+:8] !== pattern(expected_seed,packet_offset+lane))
                            $fatal(1,"Outgoing payload error at byte %0d",packet_offset+lane);
                        outgoing_bytes++;
                    end else if (packet_keep[lane] || packet_data[8*lane+:8] !== 8'd0) begin
                        $fatal(1,"Leak beyond committed byte count");
                    end
                end
                packet_offset += 8;
                if (packet_last) begin
                    transferred_packets++;
                    allow_tx = 0;
                end
            end
        end
    end

    // Write queue configuration and compare the observed acknowledgment.
    task automatic configure(
        input logic q, input logic enabled, input logic [31:0] seed, input logic bad, input logic want_ok
    );
        begin
            @(negedge clk);
            cfg_valid = 1;
            cfg_queue = q;
            cfg_enable = enabled;
            cfg_submission_base = sq_base(q);
            cfg_completion_base = bad ? sq_base(q)+32 : cq_base(q);
            cfg_payload_low = aperture_start(q);
            cfg_payload_high = aperture_end(q);
            cfg_seed = seed;
            cfg_irq_batch = q ? 8'd3 : 8'd2;
            cfg_irq_delay = q ? 32'd17 : 32'd0;
            do @(posedge clk); while (!cfg_ready);
            @(negedge clk);
            cfg_valid = 0;
            if (!cfg_ack || cfg_ok != want_ok) $fatal(1,"Config acknowledgement mismatch");
            if (want_ok) begin
                tail[q] = seed;
                host_cq[q] = seed;
                consumed[q] = seed;
                last_completion[q] = 0;
            end
            cfg_requests++;
        end
    endtask

    // Publish SQ tail and CQ head together, then check the result.
    task automatic ring_doorbell(
        input logic q, input logic [31:0] submitted, released, input logic want_ok
    );
        begin
            @(negedge clk);
            host_valid = 1;
            host_queue = q;
            host_submit_tail = submitted;
            host_complete_head = released;
            do @(posedge clk); while (!host_ready);
            @(negedge clk);
            host_valid = 0;
            if (!host_ack || host_ok !== want_ok) $fatal(1,"Doorbell validation mismatch for queue %0d",q);
            if (want_ok) begin tail[q] = submitted; host_cq[q] = released; end
            host_requests++;
        end
    endtask

    // Populate simulated memory with the fixture bytes for this transfer.
    task automatic initialize_payload(input logic [63:0] location, input integer count, seed);
        begin
            for (integer offset = 0; offset < count; offset++)
                memory.memory[int'(location)+offset] = pattern(seed, offset);
        end
    endtask

    // Encode one descriptor into host memory and publish it to the engine.
    task automatic submit(
        input logic q, input logic [7:0] op, flags, input logic [15:0] tag,
        input logic [31:0] count, input logic [63:0] addr, cookie,
        input logic [31:0] digest, reserved_word
    );
        logic [63:0] location;
        begin
            @(negedge clk);
            location = slot(sq_base(q),tail[q]);
            memory.store_word(location,{op,flags,tag,count});
            memory.store_word(location+8,addr);
            memory.store_word(location+16,cookie);
            memory.store_word(location+24,{reserved_word,digest});
            ring_doorbell(q,tail[q]+1,host_cq[q],1);
        end
    endtask

    // Wait for CQ visibility, compare all record fields, and optionally release credit.
    task automatic expect_completion(
        input logic q, input logic [7:0] status, op, input logic [15:0] tag,
        input logic [31:0] count, input logic [63:0] cookie, input logic [31:0] digest,
        input logic release_credit
    );
        logic [63:0] location, stamp;
        begin
            @(negedge clk);
            inspect_queue = q;
            do @(negedge clk); while (inspect_cq_tail != consumed[q]+1);
            if (!inspect_enabled || inspect_sq_head != consumed[q]+1 || fatal_code != 0)
                $fatal(1,"Completion and submission generations diverged");
            location = slot(cq_base(q),consumed[q]);
            if (memory.word(location) !== {status,op,tag,count} ||
                memory.word(location+8) !== cookie ||
                memory.word(location+16) !== {digest,8'(q),24'd0})
                $fatal(1,"CQ record mismatch for queue %0d tag %h: %h %h %h",q,tag,
                       memory.word(location),memory.word(location+8),memory.word(location+16));
            stamp = memory.word(location+24);
            save_completion(location);
            if (stamp <= last_completion[q] || stamp >= cycles)
                $fatal(1,"Completion clock was not monotonic or occurred after retirement");
            last_completion[q] = stamp;
            consumed[q] = consumed[q] + 1;
            compared++;
            if (status != 0) processed_errors++;
            if (release_credit)
                ring_doorbell(q,tail[q],consumed[q],1);
        end
    endtask

    // Arm the output scoreboard for one packet with a specific identity and payload.
    task automatic expect_packet(
        input logic q, input logic [15:0] tag, input logic [63:0] cookie,
        input integer count, seed
    );
        begin
            expected_packet_bytes = count;
            expected_packet_tag = tag;
            expected_cookie = cookie;
            expected_queue = q;
            expected_seed = seed;
            packet_offset = 0;
            allow_tx = 1;
            enable_tx_sink = 1;
        end
    endtask

    // Check the capture notice and accept it with programmed backpressure.
    task automatic accept_capture(input logic q,input logic [15:0] tag,input logic [31:0] count);
        begin
            wait (capture_valid);
            repeat (5) @(negedge clk);
            if ({capture_queue,capture_tag,capture_length} !== {q,tag,count})
                $fatal(1,"Wrong capture request");
            capture_ready = 1;
            @(negedge clk);
            capture_ready = 0;
        end
    endtask

    // Hold a device-stream beat and all sidebands until accepted.
    task automatic drive_packet_beat(
        input logic [63:0] data, input logic [7:0] keep,
        input logic first, last
    );
        begin
            @(negedge clk);
            s_packet_data = data;
            s_packet_keep = keep;
            s_packet_first = first;
            s_packet_last = last;
            s_packet_valid = 1;
            do @(posedge clk); while (!s_packet_ready);
            @(negedge clk);
            s_packet_valid = 0;
        end
    endtask

    // Drive a complete device packet, with a partial final beat if needed.
    task automatic complete_device_packet(input integer bytes, seed);
        logic [63:0] data;
        logic [7:0] keep;
        begin
            for (integer offset = 0; offset < bytes; offset += 8) begin
                data = '0;
                keep = '0;
                for (integer lane = 0; lane < 8; lane++)
                    if (offset+lane < bytes) begin
                        data[8*lane+:8] = pattern(seed,offset+lane);
                        keep[lane] = 1;
                    end
                repeat (offset % 3) @(negedge clk);
                drive_packet_beat(data,keep,offset == 0,offset+8 >= bytes);
            end
        end
    endtask

    // Compare stored payload bytes and verify adjacent bytes were not overwritten.
    task automatic verify_memory(input logic [63:0] location,input integer bytes,seed);
        begin
            for (integer offset = 0; offset < bytes; offset++)
                if (memory.memory[int'(location)+offset] !== pattern(seed,offset))
                    $fatal(1,"Memory write differs at offset %0d",offset);
            if (memory.memory[int'(location)-1] !== 8'haa ||
                memory.memory[int'(location)+bytes] !== 8'haa)
                $fatal(1,"AXI strobes overwrote a neighbor outside exact packet");
        end
    endtask

    // Apply a synchronous reset and return the interface drivers to idle.
    task automatic reset_core;
        begin
            @(negedge clk);
            reset = 1;
            inject_read_error = '1;
            inject_write_error = '1;
            inject_bad_last = '1;
            enable_tx_sink = 0;
            block_read_address = 0;
            block_write_address = 0;
            block_write_response = 0;
            memory.clear_memory(8'haa);
            repeat (7) @(negedge clk);
            reset = 0;
            repeat (2) @(negedge clk);
        end
    endtask

    // Save checked simulation pins when requested by the waveform target.
    initial begin : waveform_capture
        string waves_path;
        if ($value$plusargs("waves=%s", waves_path)) begin
            $dumpfile(waves_path);
            $dumpvars(1, tb_host_dma_queues);
            $dumpvars(1, tb_host_dma_queues.dut);
        end
    end

`include "tb_dma_readback.svh"
`include "tb_dma_scatter.svh"
`include "tb_dma_checkpoint.svh"

    task automatic run_isolated_access;
        logic [63:0] peer_payload;
        int prior_addresses;
        configure(0,1,3,0,1);
        configure(1,1,7,0,1);
        @(negedge clk);
        // The new queue-zero payload interval is locally valid but owns bytes of SQ 1.
        cfg_valid = 1;
        cfg_queue = 0;
        cfg_enable = 1;
        cfg_submission_base = sq_base(0);
        cfg_completion_base = cq_base(0);
        cfg_payload_low = sq_base(1);
        cfg_payload_high = sq_base(1) + 8;
        cfg_seed = 16;
        @(posedge clk);
        if (!cfg_ready) $fatal(1,"isolated configuration was not sampled");
        @(negedge clk);
        cfg_valid = 0;
        if (!cfg_ack || cfg_ok) $fatal(1,"top-level isolation parameter allowed peer SQ ownership");

        peer_payload = aperture_start(1);
        initialize_payload(peer_payload,13,721);
        prior_addresses = memory.ar_count;
        submit(0,4,0,16'h0311,13,peer_payload,64'h177,0,0);
        expect_completion(0,3,4,16'h0311,0,64'h177,0,1);
        if (memory.ar_count != prior_addresses + 1)
            $fatal(1,"peer payload request entered the memory data path");
        submit(1,4,0,16'h0312,13,peer_payload,64'h188,0,0);
        expect_completion(1,0,4,16'h0312,13,64'h188,reference_crc(721,13),1);
        if (memory.ar_count != prior_addresses + 3)
            $fatal(1,"peer owner could not access its own payload bytes");
        $display("PASS DMA isolation integration: rejects peer SQ claim and checks address ownership on READBACK");
    endtask

    initial begin : run
        logic [31:0] crc_a, crc_b;
        logic [31:0] saved_sq, saved_cq;
        integer before_read, before_write;
        open_capture();
        integrity_run = $test$plusargs("readback") || $test$plusargs("isolation-access");
        reset_core();
        if ($test$plusargs("checkpoint")) begin
            run_checkpoint();
            $finish;
        end
        if ($test$plusargs("segment-capacity")) begin
            run_segment_capacity();
            $finish;
        end
        if ($test$plusargs("scatter")) begin
            run_scatter();
            if (packet_capture) $fclose(packet_capture);
            if (completion_capture) $fclose(completion_capture);
            $finish;
        end
        if ($test$plusargs("isolation-access")) begin
            run_isolated_access();
            $finish;
        end
        if (integrity_run) begin
            run_readback();
            if (packet_capture) $fclose(packet_capture);
            if (completion_capture) $fclose(completion_capture);
            $finish;
        end
        configure(1,1,0,1,0);
        configure(0,1,32'hfffffffd,0,1);
        configure(1,1,32'd25,0,1);
        ring_doorbell(0,tail[0]+SLOTS+1,host_cq[0],0);
        ring_doorbell(1,tail[1],host_cq[1]+1,0);

        initialize_payload(64'h0ff0,139,1331);
        crc_a = reference_crc(1331,139);
        expect_packet(0,16'h015f,64'h55557777,139,1331);
        submit(0,1,3,16'h015f,139,64'h0ff0,64'h55557777,crc_a,0);
        expect_completion(0,0,1,16'h015f,139,64'h55557777,crc_a,1);
        if (allow_tx || !irq[0] || pull_bytes != 139)
            $fatal(1,"Forced interrupt or first byte counter failed");
        @(negedge clk);
        irq_ack = 2'b01;
        @(negedge clk);
        irq_ack = 0;
        if (irq[0]) $fatal(1,"Interrupt acknowledgment did not clear level");

        crc_b = reference_crc(722,129);
        submit(1,2,2,16'h018d,129,64'h5ff8,64'h489da,crc_b,0);
        accept_capture(1,16'h018d,129);
        complete_device_packet(129,722);
        expect_completion(1,0,2,16'h018d,129,64'h489da,crc_b,1);
        verify_memory(64'h5ff8,129,722);
        repeat (24) @(negedge clk);
        if (!irq[1] || push_bytes != 129)
            $fatal(1,"Interrupt timer or second byte counter failed");

        submit(0,1,2,16'hd000,139,64'h0ff0,17,crc_a^1,0);
        expect_completion(0,6,1,16'hd000,0,17,crc_a,1);

        before_write = memory.aw_count;
        submit(1,2,2,16'hd001,29,64'h7900,18,32'h11111111,0);
        accept_capture(1,16'hd001,29);
        complete_device_packet(29,14);
        expect_completion(1,6,2,16'hd001,0,18,reference_crc(14,29),1);
        if (memory.aw_count != before_write+1 || memory.memory[16'h7900] != 8'haa)
            $fatal(1,"CRC rejection wrote payload memory");

        before_read = memory.ar_count;
        submit(0,1,0,16'hd002,33,64'h47f0,19,0,0);
        expect_completion(0,3,1,16'hd002,0,19,0,1);
        if (memory.ar_count != before_read+1)
            $fatal(1,"Out-of-range PULL leaked a memory command");

        submit(1,1,0,16'hd003,16,64'h7000,20,0,1);
        expect_completion(1,2,1,16'hd003,0,20,0,1);
        submit(1,49,0,16'hd004,0,0,21,0,0);
        expect_completion(1,1,49,16'hd004,0,21,0,1);
        submit(0,3,2,16'hd005,0,0,22,0,0);
        expect_completion(0,2,3,16'hd005,0,22,0,1);
        if (tail[0] != 1 || consumed[0] != 1)
            $fatal(1,"Submission and completion sequence counters did not wrap");

        before_write = memory.aw_count;
        submit(1,2,0,16'hd006,25,64'h7100,23,0,0);
        accept_capture(1,16'hd006,25);
        drive_packet_beat(64'h12,8'h02,1,0);
        drive_packet_beat(64'h1234,8'h0f,0,1);
        expect_completion(1,7,2,16'hd006,0,23,0,1);
        if (memory.aw_count != before_write+1 || memory.memory[16'h7100] != 8'haa)
            $fatal(1,"Malformed capture escaped validation");

        submit(1,2,0,16'hd007,40,64'h7400,24,0,0);
        accept_capture(1,16'hd007,40);
        expect_completion(1,8,2,16'hd007,0,24,0,1);
        if (aborted_captures != 1) $fatal(1,"Capture timeout did not notify producer");

        inject_read_error = 64'h1000;
        submit(0,1,0,16'hd008,139,64'h0ff0,25,0,0);
        expect_completion(0,4,1,16'hd008,0,25,0,1);
        inject_read_error = slot(sq_base(0),tail[0]) + 8;
        submit(0,3,0,16'hd009,0,0,26,0,0);
        expect_completion(0,4,0,0,0,0,0,1);
        inject_read_error = '1;

        inject_write_error = 64'h7750;
        submit(1,2,0,16'hd00a,154,64'h7710,27,0,0);
        accept_capture(1,16'hd00a,154);
        complete_device_packet(154,411);
        expect_completion(1,5,2,16'hd00a,0,27,reference_crc(411,154),1);
        inject_write_error = '1;

        @(negedge clk);
        inspect_queue = 0;
        #1;
        saved_sq = inspect_sq_head;
        saved_cq = inspect_cq_tail;
        block_write_response = 1;
        submit(0,3,0,16'hd00b,0,0,28,0,0);
        wait (memory.write_response_wait);
        repeat (10) @(negedge clk);
        if (inspect_sq_head != saved_sq || inspect_cq_tail != saved_cq ||
            completed != 32'(compared))
            $fatal(1,"Publication preceded its successful AXI B response");
        block_write_response = 0;
        expect_completion(0,0,3,16'hd00b,0,28,0,1);

        if (memory.page_ends_read < 2 || !memory.page_ends_write || !memory.short_writes ||
            !memory.response_stalls || !memory.write_stalls || !packet_stalls || !capture_stalls ||
            memory.read_faults != 2 || memory.write_faults != 1 ||
            transferred_packets != 1 || outgoing_bytes != 139)
            $fatal(1,"Required data, stall or fault coverage was not reached");
        if (completed !== 32'(compared) || rejected !== 32'(processed_errors))
            $fatal(1,"Completion counters disagree with observed records");
        $display("PASS DMA end-to-end: %0d CQ records, %0d errors, %0d read + %0d write bursts",
                 compared,processed_errors,memory.ar_count,memory.aw_count);
        $display("  Packet/capture/AXI stalls %0d/%0d/%0d/%0d/%0d; %0d config and %0d doorbells",
                 packet_stalls,capture_stalls,memory.read_stalls,memory.write_stalls,
                 memory.response_stalls,cfg_requests,host_requests);

        // Once user-visible data might have committed, a CQ bus failure must
        // halt instead of retrying a descriptor with possible side effects.
        inject_write_error = slot(cq_base(0),consumed[0]);
        saved_sq = inspect_sq_head;
        saved_cq = inspect_cq_tail;
        submit(0,3,1,16'hd00c,0,0,29,0,0);
        wait (fatal_code == 2);
        repeat (8) @(negedge clk);
        if (inspect_sq_head != saved_sq || inspect_cq_tail != saved_cq || cfg_ready ||
            completed != 32'(compared))
            $fatal(1,"Completion write failure was retired or restartable");

        reset_core();
        configure(0,1,32'd38,0,1);
        inject_bad_last = slot(sq_base(0),tail[0]);
        submit(0,3,0,16'hd00d,0,0,30,0,0);
        wait (fatal_code == 1);
        repeat (8) @(negedge clk);
        if (completed || rejected || inspect_sq_head != 38 || inspect_cq_tail != 38 ||
            memory.aw_count != 0)
            $fatal(1,"Malformed AXI burst leaked a retirement");
        if (packet_capture) $fclose(packet_capture);
        if (completion_capture) $fclose(completion_capture);
        $display("PASS DMA fail-stop: completion B error and early RLAST, recovery by shared reset");
        $finish;
    end

    initial begin #1600000; $fatal(1,"DMA integration timeout"); end
endmodule
