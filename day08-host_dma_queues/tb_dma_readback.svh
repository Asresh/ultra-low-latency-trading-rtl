    // Alternate run shares the AXI and host transaction monitors of the legacy bench.
    task automatic assert_held_cursors(input logic [31:0] sq, cq);
        if (inspect_sq_head !== sq || inspect_cq_tail !== cq || completed !== 32'(compared))
            $fatal(1,"Integrity operation retired before its CQ response");
    endtask

    task automatic run_readback;
        localparam logic [7:0] PUSH = 2, READBACK = 4;
        localparam logic [7:0] OK = 0, BAD_DESC = 2, ADDRESS = 3, READ_ERROR = 4,
                               WRITE_ERROR = 5, BAD_CRC = 6, VERIFY_ERROR = 9;
        localparam logic [63:0] CROSS_PAGE = 64'h0ff8;
        localparam logic [63:0] MUTATE_ADDRESS = 64'h2230;
        localparam integer CROSS_COUNT = 129, CROSS_SEED = 774;
        logic [31:0] expected, observed, prior_sq, prior_cq;
        logic [63:0] original_word;
        integer rd_start, wr_start, b_start;
        integer count, fixture_seed;
        logic [63:0] address;

        configure(0,1,32'hfffffffd,0,1);
        configure(1,1,32'd11,0,1);
        inspect_queue = 0;
        original_word = 64'haa55deadbeefeedd;
        expected = reference_crc(CROSS_SEED,CROSS_COUNT);
        rd_start = memory.ar_count;
        wr_start = memory.aw_count;
        b_start = memory.b_count;
        prior_sq = inspect_sq_head;
        prior_cq = inspect_cq_tail;
        block_write_response = 1;
        submit(0,PUSH,7,16'h0100,CROSS_COUNT,CROSS_PAGE,64'h0051,expected,0);
        accept_capture(0,16'h0100,CROSS_COUNT);
        complete_device_packet(CROSS_COUNT,CROSS_SEED);
        wait (memory.write_response_wait);
        repeat (9) begin
            @(negedge clk);
            assert_held_cursors(prior_sq,prior_cq);
            if (arvalid || irq[0] || push_bytes != 0)
                $fatal(1,"Verification read or publication preceded payload B");
        end
        block_write_response = 0;
        wait (arvalid && araddr == CROSS_PAGE);
        if (memory.b_count != b_start+3)
            $fatal(1,"Readback preceded one of three page/cap-limited payload B responses");
        // Permit the verification read, then hold only the completion B.
        block_write_response = 1;
        wait (memory.write_response_wait);
        repeat (7) begin
            @(negedge clk);
            assert_held_cursors(prior_sq,prior_cq);
            if (push_bytes != 0 || irq[0]) $fatal(1,"CQ stall exposed byte count or IRQ");
        end
        block_write_response = 0;
        expect_completion(0,OK,PUSH,16'h0100,CROSS_COUNT,64'h0051,expected,1);
        if (!irq[0] || memory.ar_count != rd_start+4 || memory.aw_count != wr_start+4 ||
            push_bytes != CROSS_COUNT || !memory.short_writes)
            $fatal(1,"Verified PUSH did not use the expected bursts, partial write or IRQ");
        verify_memory(CROSS_PAGE,CROSS_COUNT,CROSS_SEED);
        @(negedge clk);
        irq_ack = 2'b01;
        @(negedge clk);
        irq_ack = 0;

        // An existing memory range can be hashed or checked without a stream packet.
        rd_start = memory.ar_count;
        wr_start = memory.aw_count;
        submit(0,READBACK,0,16'h0101,CROSS_COUNT,CROSS_PAGE,64'h0052,0,0);
        expect_completion(0,OK,READBACK,16'h0101,CROSS_COUNT,64'h0052,expected,1);
        if (memory.ar_count != rd_start+4 || memory.aw_count != wr_start+1)
            $fatal(1,"Standalone READBACK required unexpected bus traffic");
        submit(0,READBACK,2,16'h0102,CROSS_COUNT,CROSS_PAGE,64'h0053,expected^1,0);
        expect_completion(0,BAD_CRC,READBACK,16'h0102,0,64'h0053,expected,1);
        if (consumed[0] != 0) $fatal(1,"Readback CQ did not wrap its absolute cursor");

        // A successful write response followed by changed bytes is distinguishable
        // from both capture CRC rejection and an unsuccessful read response.
        submit(0,PUSH,4,16'h0103,8,MUTATE_ADDRESS,64'h0054,0,0);
        accept_capture(0,16'h0103,8);
        block_write_response = 1;
        drive_packet_beat(original_word,8'hff,1,1);
        wait (memory.write_response_wait);
        @(negedge clk);
        expected = 32'h2a9944c9;
        observed = 32'h79a2c113;
        if (memory.word(MUTATE_ADDRESS) !== original_word)
            $fatal(1,"Corruption fixture failed to capture original bytes");
        memory.store_word(MUTATE_ADDRESS, original_word ^ 64'h80);
        block_write_response = 0;
        expect_completion(0,VERIFY_ERROR,PUSH,16'h0103,0,64'h0054,observed,1);
        submit(0,READBACK,2,16'h0104,8,MUTATE_ADDRESS,64'h0055,expected,0);
        expect_completion(0,BAD_CRC,READBACK,16'h0104,0,64'h0055,observed,1);

        inject_read_error = 64'h2638;
        submit(0,PUSH,4,16'h0105,9,64'h2630,64'h0056,0,0);
        accept_capture(0,16'h0105,9);
        complete_device_packet(9,111);
        expect_completion(0,READ_ERROR,PUSH,16'h0105,0,64'h0056,0,1);
        verify_memory(64'h2630,9,111);
        submit(0,READBACK,0,16'h0106,9,64'h2630,64'h0057,0,0);
        expect_completion(0,READ_ERROR,READBACK,16'h0106,0,64'h0057,0,1);
        inject_read_error = '1;

        rd_start = memory.ar_count;
        inject_write_error = 64'h2720;
        submit(0,PUSH,4,16'h0107,13,64'h2720,64'h0058,0,0);
        accept_capture(0,16'h0107,13);
        complete_device_packet(13,74);
        expect_completion(0,WRITE_ERROR,PUSH,16'h0107,0,64'h0058,reference_crc(74,13),1);
        inject_write_error = '1;
        if (memory.ar_count != rd_start+1) $fatal(1,"Verification followed a failed payload write");

        wr_start = memory.aw_count;
        rd_start = memory.ar_count;
        submit(0,PUSH,6,16'h0108,7,64'h2860,64'h0059,32'h555faa5f,0);
        accept_capture(0,16'h0108,7);
        complete_device_packet(7,353);
        expect_completion(0,BAD_CRC,PUSH,16'h0108,0,64'h0059,reference_crc(353,7),1);
        if (memory.ar_count != rd_start+1 || memory.aw_count != wr_start+1 ||
            memory.word(64'h2860) !== 64'haaaaaaaaaaaaaaaa)
            $fatal(1,"A failed capture comparison touched the payload aperture");

        rd_start = memory.ar_count;
        submit(0,READBACK,4,16'h0109,12,64'h2860,64'h005a,0,0);
        expect_completion(0,BAD_DESC,READBACK,16'h0109,0,64'h005a,0,1);
        submit(0,READBACK,0,16'h010a,9,64'h47f8,64'h005b,0,0);
        expect_completion(0,ADDRESS,READBACK,16'h010a,0,64'h005b,0,1);
        if (memory.ar_count != rd_start+2 || push_bytes != CROSS_COUNT)
            $fatal(1,"Rejected descriptors issued a payload read or incremented PUSH bytes");

        // Exercise every partial tail size, exact beats and the maximum payload.
        for (int tail_bytes = 1; tail_bytes <= 9; tail_bytes++) begin
            count = tail_bytes == 9 ? MAX_PACKET : tail_bytes;
            address = 64'h6000 + 64'(tail_bytes * 576);
            fixture_seed = 5000 + tail_bytes * 11;
            expected = reference_crc(fixture_seed,count);
            submit(1,PUSH,4,16'(16'h0200+tail_bytes),32'(count),address,address,0,0);
            accept_capture(1,16'(16'h0200+tail_bytes),32'(count));
            complete_device_packet(count,fixture_seed);
            expect_completion(1,OK,PUSH,16'(16'h0200+tail_bytes),32'(count),address,expected,1);
            verify_memory(address,count,fixture_seed);
            submit(1,READBACK,2,16'(16'h0300+tail_bytes),32'(count),address,address,expected,0);
            expect_completion(1,OK,READBACK,16'(16'h0300+tail_bytes),32'(count),address,expected,1);
        end
        if (completed != 29 || rejected != 9 || pull_bytes != 0 || transferred_packets != 0 ||
            push_bytes != CROSS_COUNT + 36 + MAX_PACKET || packet_valid || capture_valid)
            $fatal(1,"Readback fixture totals or stream isolation differ");
        $display("PASS DMA integrity: %0d CQ records, %0d errors, %0d verified PUSH bytes",compared,processed_errors,push_bytes);

        // A malformed verification RLAST after successful payload B is fail-stop.
        inspect_queue = 0;
        @(negedge clk);
        prior_sq = inspect_sq_head;
        prior_cq = inspect_cq_tail;
        wr_start = memory.aw_count;
        inject_bad_last = 64'h2900;
        submit(0,PUSH,4,16'h0400,9,64'h2900,64'h005c,0,0);
        accept_capture(0,16'h0400,9);
        complete_device_packet(9,912);
        wait (fatal_code == 1);
        repeat (5) @(negedge clk);
        assert_held_cursors(prior_sq,prior_cq);
        verify_memory(64'h2900,9,912);
        if (memory.aw_count != wr_start+1)
            $fatal(1,"Malformed verification read attempted a completion write");
        $display("PASS DMA integrity fail-stop: malformed verification RLAST with payload present");
    endtask
