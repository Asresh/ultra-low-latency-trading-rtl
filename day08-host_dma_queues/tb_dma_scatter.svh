    task automatic load_segment(input logic [63:0] list_base, input integer index,
                                input logic [63:0] base, input logic [31:0] count, reserved);
        memory.store_word(list_base+64'(16*index),base);
        memory.store_word(list_base+64'(16*index+8),{reserved,count});
    endtask

    task automatic run_segment_capacity;
        localparam logic [63:0] LIST = 64'h1ff8;
        localparam int LENGTH = 8*(TEST_SEGMENTS-1)+3;
        logic [63:0] base;
        int count;
        configure(0,1,46,0,1);
        for (int index = 0; index < TEST_SEGMENTS; index++) begin
            base = 64'h2800 + 64'((TEST_SEGMENTS-index)*32);
            count = index == TEST_SEGMENTS-1 ? 3 : 8;
            load_segment(LIST,index,base,32'(count),0);
            write_chunk(base,count,67,index*8);
        end
        expect_packet(0,16'h0550,8, LENGTH,67);
        submit(0,5,2,16'h0550,32'(LENGTH),LIST,8,reference_crc(67,LENGTH),32'(TEST_SEGMENTS));
        expect_completion(0,0,5,16'h0550,32'(LENGTH),8,reference_crc(67,LENGTH),1);
        submit(0,6,4,16'h0551,32'(LENGTH),LIST,9,0,32'(TEST_SEGMENTS));
        accept_capture(0,16'h0551,32'(LENGTH));
        complete_device_packet(LENGTH,171);
        expect_completion(0,0,6,16'h0551,32'(LENGTH),9,reference_crc(171,LENGTH),1);
        for (int index = 0; index < TEST_SEGMENTS; index++) begin
            base = 64'h2800 + 64'((TEST_SEGMENTS-index)*32);
            count = index == TEST_SEGMENTS-1 ? 3 : 8;
            verify_chunk(base,count,171,index*8);
        end
        if (pull_bytes != LENGTH || push_bytes != LENGTH || transferred_packets != 1)
            $fatal(1,"Maximum-size list did not execute one packet each direction");
        $display("PASS DMA segment capacity %0d with descriptor lists crossing an AXI page",TEST_SEGMENTS);
    endtask

    task automatic write_chunk(input logic [63:0] base, input integer length, seed, offset);
        for (int i = 0; i < length; i++)
            memory.memory[int'(base)+i] = pattern(seed,offset+i);
    endtask

    task automatic verify_chunk(input logic [63:0] base, input integer length, seed, offset);
        for (int i = 0; i < length; i++)
            if (memory.memory[int'(base)+i] !== pattern(seed,offset+i))
                $fatal(1,"Scatter byte mismatch at %h offset %0d", base, i);
        if (memory.memory[int'(base)-1] !== 8'haa || memory.memory[int'(base)+length] !== 8'haa)
            $fatal(1,"Scatter crossed the exact byte footprint at %h",base);
    endtask

    task automatic run_scatter;
        localparam logic [7:0] GATHER=5, SCATTER=6, BAD_DESC=2, ADDRESS=3, READ_ERROR=4,
                               WRITE_ERROR=5, CRC_ERROR=6, VERIFY_ERROR=9, BAD_SEGMENT=10;
        localparam logic [63:0] GATHER_TABLE=64'h1ff8, SCATTER_TABLE=64'h5ff0;
        localparam logic [63:0] G0=64'h2ff8, G1=64'h2600, G2=64'h3400;
        localparam logic [63:0] S0=64'h7ff0, S1=64'h6ff8, BAD_TABLE=64'h4100;
        logic [31:0] gather_crc, scatter_crc;
        logic [31:0] head_before, cq_before;
        int ar_before, aw_before, b_before;
        configure(0,1,32'hffff_fffb,0,1);
        configure(1,1,7,0,1);

        // Both the list and one data segment cross a physical AXI page.
        load_segment(GATHER_TABLE,0,G0,16,0);
        load_segment(GATHER_TABLE,1,G1,128,0);
        load_segment(GATHER_TABLE,2,G2,19,0);
        write_chunk(G0,16,88,0);
        write_chunk(G1,128,88,16);
        write_chunk(G2,19,88,144);
        gather_crc = reference_crc(88,163);
        ar_before = memory.ar_count;
        aw_before = memory.aw_count;
        expect_packet(0,16'h0500,64'h1101,163,88);
        submit(0,GATHER,2,16'h0500,163,GATHER_TABLE,64'h1101,gather_crc,3);
        expect_completion(0,0,GATHER,16'h0500,163,64'h1101,gather_crc,1);
        if (transferred_packets != 1 || outgoing_bytes != 163 || memory.ar_count != ar_before+8 ||
            memory.aw_count != aw_before+1 || pull_bytes != 163)
            $fatal(1,"Gather did not retire exactly one concatenated frame");

        load_segment(SCATTER_TABLE,0,S0,8,0);
        load_segment(SCATTER_TABLE,1,S1,145,0);
        scatter_crc = reference_crc(117,153);
        inspect_queue = 1;
        @(negedge clk);
        head_before = inspect_sq_head;
        cq_before = inspect_cq_tail;
        ar_before = memory.ar_count;
        aw_before = memory.aw_count;
        b_before = memory.b_count;
        block_write_response = 1;
        submit(1,SCATTER,7,16'h0501,153,SCATTER_TABLE,64'h1202,scatter_crc,2);
        accept_capture(1,16'h0501,153);
        complete_device_packet(153,117);
        wait (memory.write_response_wait);
        repeat (8) begin
            @(negedge clk);
            assert_held_cursors(head_before,cq_before);
            if (arvalid || awvalid || memory.ar_count != ar_before+3 || push_bytes != 0)
                $fatal(1,"Second segment, verification, or CQ advanced under the first payload B stall");
        end
        block_write_response = 0;
        wait (arvalid && araddr == S0);
        if (memory.b_count != b_before+5)
            $fatal(1,"Readback issued before all five segmented payload responses");
        block_write_response = 1;
        wait (memory.write_response_wait);
        repeat (7) begin
            @(negedge clk);
            assert_held_cursors(head_before,cq_before);
            if (irq[1]) $fatal(1,"IRQ preceded the held completion response");
        end
        block_write_response = 0;
        expect_completion(1,0,SCATTER,16'h0501,153,64'h1202,scatter_crc,1);
        if (!irq[1] || memory.ar_count != ar_before+8 || memory.aw_count != aw_before+6 ||
            push_bytes != 153 || memory.short_writes != 1)
            $fatal(1,"Verified scatter published the wrong counts");
        verify_chunk(S0,8,117,0);
        verify_chunk(S1,145,117,8);

        // A later gather R error suppresses output and prevents any remaining segment read.
        ar_before = memory.ar_count;
        inject_read_error = G1+64;
        submit(0,GATHER,0,16'h0502,163,GATHER_TABLE,64'h1103,0,3);
        expect_completion(0,READ_ERROR,GATHER,16'h0502,0,64'h1103,0,1);
        inject_read_error = '1;
        if (memory.ar_count != ar_before+7 || transferred_packets != 1)
            $fatal(1,"Gather read error accessed the last segment or leaked a packet");

        submit(0,GATHER,2,16'h0503,163,GATHER_TABLE,64'h1104,gather_crc^1,3);
        expect_completion(0,CRC_ERROR,GATHER,16'h0503,0,64'h1104,gather_crc,1);
        if (transferred_packets != 1) $fatal(1,"Gather CRC rejection leaked a packet");
        submit(0,7,2,16'h0504,163,GATHER_TABLE,64'h1105,gather_crc,3);
        expect_completion(0,0,7,16'h0504,163,64'h1105,gather_crc,1);
        if (packet_valid || transferred_packets != 1 || pull_bytes != 163)
            $fatal(1,"Segment digest emitted bytes or changed the gather statistic");

        // Several independent list faults appear in the final row, after a legal first row.
        load_segment(BAD_TABLE,0,64'h3600,16,0);
        for (int variant = 0; variant < 7; variant++) begin
            load_segment(BAD_TABLE,1,64'h3710,9,0);
            case (variant)
                0: load_segment(BAD_TABLE,1,64'h3608,9,0); // Overlap a prior segment.
                1: load_segment(BAD_TABLE,1,BAD_TABLE+8,9,0); // Overlap its list.
                2: load_segment(BAD_TABLE,1,64'h47f8,9,0); // Rounded outside the aperture.
                3: load_segment(BAD_TABLE,1,64'h3710,9,1);
                4: load_segment(BAD_TABLE,1,64'h3711,9,0);
                5: load_segment(BAD_TABLE,1,64'h3710,7,0); // Aggregate length mismatch.
                default: load_segment(BAD_TABLE,1,64'h3710,0,0);
            endcase
            ar_before = memory.ar_count;
            aw_before = memory.aw_count;
            submit(0,SCATTER,0,16'(16'h0510+variant),25,BAD_TABLE,64'(16'h1300+variant),0,2);
            expect_completion(0,BAD_SEGMENT,SCATTER,16'(16'h0510+variant),0,64'(16'h1300+variant),0,1);
            if (memory.ar_count != ar_before+2 || memory.aw_count != aw_before+1 ||
                memory.word(64'h3600) != 64'haaaaaaaaaaaaaaaa)
                $fatal(1,"Invalid table touched a payload region");
        end
        load_segment(BAD_TABLE,0,64'h3600,15,0);
        load_segment(BAD_TABLE,1,64'h3710,10,0);
        submit(0,GATHER,0,16'h0517,25,BAD_TABLE,64'h1307,0,2);
        expect_completion(0,BAD_SEGMENT,GATHER,16'h0517,0,64'h1307,0,1);

        ar_before = memory.ar_count;
        submit(0,GATHER,0,16'h0520,25,GATHER_TABLE,64'h1310,0,0);
        expect_completion(0,BAD_DESC,GATHER,16'h0520,0,64'h1310,0,1);
        submit(0,SCATTER,0,16'h0521,25,GATHER_TABLE,64'h1311,0,32'(TEST_SEGMENTS+1));
        expect_completion(0,BAD_DESC,SCATTER,16'h0521,0,64'h1311,0,1);
        submit(0,GATHER,0,16'h0522,25,64'h47f8,64'h1312,0,1);
        expect_completion(0,ADDRESS,GATHER,16'h0522,0,64'h1312,0,1);
        if (memory.ar_count != ar_before+3) $fatal(1,"Invalid header issued a segment-table read");

        inject_read_error = GATHER_TABLE+8;
        submit(0,SCATTER,0,16'h0523,163,GATHER_TABLE,64'h1313,0,3);
        expect_completion(0,READ_ERROR,SCATTER,16'h0523,0,64'h1313,0,1);
        inject_read_error = '1;

        // The second payload segment fails B. Completed earlier writes survive.
        load_segment(SCATTER_TABLE,0,64'h6100,8,0);
        load_segment(SCATTER_TABLE,1,64'h6200,8,0);
        load_segment(SCATTER_TABLE,2,64'h6300,9,0);
        ar_before = memory.ar_count;
        aw_before = memory.aw_count;
        inject_write_error = 64'h6200;
        submit(1,SCATTER,4,16'h0530,25,SCATTER_TABLE,64'h1400,0,3);
        accept_capture(1,16'h0530,25);
        complete_device_packet(25,121);
        expect_completion(1,WRITE_ERROR,SCATTER,16'h0530,0,64'h1400,reference_crc(121,25),1);
        inject_write_error = '1;
        verify_chunk(64'h6100,8,121,0);
        if (memory.word(64'h6300) != 64'haaaaaaaaaaaaaaaa ||
            memory.ar_count != ar_before+3 || memory.aw_count != aw_before+3 || push_bytes != 153)
            $fatal(1,"Failed scatter continued or counted a completed write");

        // Read comparison across the second segment catches a mutation after its final W.
        load_segment(SCATTER_TABLE,0,64'h6400,8,0);
        load_segment(SCATTER_TABLE,1,64'h6500,8,0);
        submit(1,SCATTER,4,16'h0531,16,SCATTER_TABLE,64'h1401,0,2);
        accept_capture(1,16'h0531,16);
        drive_packet_beat(64'h4847464544434241,8'hff,1,0); // ABCDEFGH
        drive_packet_beat(64'h504f4e4d4c4b4a49,8'hff,0,1); // IJKLMNOP
        wait (awvalid && awaddr == 64'h6500);
        block_write_response = 1;
        wait (memory.write_response_wait);
        @(negedge clk);
        memory.memory[16'h6500] = 8'h78; // ABCDEFGHxJKLMNOP
        block_write_response = 0;
        expect_completion(1,VERIFY_ERROR,SCATTER,16'h0531,0,64'h1401,32'ha98facae,1);

        load_segment(BAD_TABLE,0,64'h4300,13,0);
        initialize_payload(64'h4300,13,141);
        expect_packet(0,16'h0540,64'h1500,13,141);
        submit(0,GATHER,0,16'h0540,13,BAD_TABLE,64'h1500,0,1);
        expect_completion(0,0,GATHER,16'h0540,13,64'h1500,reference_crc(141,13),1);
        if (pull_bytes != 176 || outgoing_bytes != 176 || transferred_packets != 2 ||
            push_bytes != 153 || completed != 20 || rejected != 16)
            $fatal(1,"Segmented completion or byte totals disagree");

        // Reset brings the worker back after malformed list framing without a CQ write.
        inspect_queue = 0;
        @(negedge clk);
        head_before = inspect_sq_head;
        cq_before = inspect_cq_tail;
        aw_before = memory.aw_count;
        inject_bad_last = GATHER_TABLE;
        submit(0,GATHER,0,16'h0541,163,GATHER_TABLE,64'h1501,0,3);
        wait (fatal_code == 1);
        repeat (5) @(negedge clk);
        assert_held_cursors(head_before,cq_before);
        if (memory.aw_count != aw_before) $fatal(1,"Malformed segment table tried to retire");
        reset_core();
        configure(0,1,31,0,1);
        if (fatal_code || completed) $fatal(1,"Reset failed after segmented fail-stop");
        $display("PASS DMA segmented: %0d CQ records, %0d errors, 176 gathered and 153 scattered bytes",compared,processed_errors);
    endtask
