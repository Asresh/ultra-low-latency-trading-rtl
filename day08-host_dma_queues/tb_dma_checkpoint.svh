    integer checkpoint_phase = 0;

    task automatic store_checkpoint_submission(
        input logic [31:0] cursor,
        input logic [7:0] operation, flags,
        input logic [15:0] tag,
        input logic [63:0] cookie
    );
        logic [63:0] address;
        address = slot(sq_base(0), cursor);
        memory.store_word(address,{operation,flags,tag,32'b0});
        memory.store_word(address+8,0);
        memory.store_word(address+16,cookie);
        memory.store_word(address+24,0);
    endtask

    task automatic record_checkpoint(
        input string directory, name,
        input integer meta,
        input logic [31:0] submission_tail, completion_head, retired
    );
        integer submission_file, completion_file;
        if (!inspect_enabled || fatal_code ||
            inspect_sq_tail !== submission_tail || inspect_cq_head !== completion_head ||
            inspect_sq_head !== retired || inspect_cq_tail !== retired)
            $fatal(1,"Checkpoint %s did not observe the expected committed window",name);
        submission_file = $fopen({directory,"/",name,".sq.bin"},"wb");
        completion_file = $fopen({directory,"/",name,".cq.bin"},"wb");
        if (!submission_file || !completion_file)
            $fatal(1,"Could not write checkpoint ring images to %s",directory);
        for (integer index = 0; index < SLOTS*32; index++) begin
            $fwrite(submission_file,"%c",memory.memory[int'(sq_base(0))+index]);
            $fwrite(completion_file,"%c",memory.memory[int'(cq_base(0))+index]);
        end
        $fclose(submission_file);
        $fclose(completion_file);
        $fdisplay(meta,"%s %08h %08h %08h %08h %h %h",name,inspect_sq_head,
                  inspect_sq_tail,inspect_cq_head,inspect_cq_tail,fatal_code,inspect_enabled);
        checkpoint_phase++;
    endtask

    task automatic run_checkpoint;
        localparam logic [31:0] START = 32'hfffffffe;
        string directory;
        integer metadata;
        if (!$value$plusargs("checkpoint-dir=%s",directory))
            $fatal(1,"Checkpoint fixture needs +checkpoint-dir");
        metadata = $fopen({directory,"/cursors.txt"},"w");
        if (!metadata)
            $fatal(1,"Could not write checkpoint cursor metadata");

        configure(0,1,START,0,1);
        record_checkpoint(directory,"initial",metadata,START,START,START);

        // Post two slots across rollover, then stall the completion write
        // response once all four CQ words have landed in the memory model.
        @(negedge clk);
        store_checkpoint_submission(START,3,0,16'h1001,64'hc0ffee0000000001);
        store_checkpoint_submission(START+1,8'ha5,0,16'h1002,64'hc0ffee0000000002);
        block_write_response = 1;
        ring_doorbell(0,0,START,1);
        do @(negedge clk); while (!memory.write_response_wait);
        if (bvalid || memory.word(slot(cq_base(0),START)) !== 64'h0003100100000000)
            $fatal(1,"Capture must contain a written but uncommitted completion");
        record_checkpoint(directory,"first_held",metadata,0,START,START);
        repeat (5) @(negedge clk);
        block_write_response = 0;
        expect_completion(0,0,3,16'h1001,0,64'hc0ffee0000000001,0,0);
        expect_completion(0,1,8'ha5,16'h1002,0,64'hc0ffee0000000002,0,0);
        record_checkpoint(directory,"first_retired",metadata,0,START,0);

        // Observe a crash after a joint release and submission doorbell.
        @(negedge clk);
        store_checkpoint_submission(0,3,1,16'h1003,64'hc0ffee0000000003);
        block_write_response = 1;
        ring_doorbell(0,1,0,1);
        do @(negedge clk); while (!memory.write_response_wait);
        record_checkpoint(directory,"next_held",metadata,1,0,0);
        repeat (4) @(negedge clk);
        block_write_response = 0;
        expect_completion(0,0,3,16'h1003,0,64'hc0ffee0000000003,0,0);
        record_checkpoint(directory,"next_retired",metadata,1,0,1);
        ring_doorbell(0,1,1,1);
        record_checkpoint(directory,"complete",metadata,1,1,1);
        $fclose(metadata);
        $display("PASS checkpoint fixture: %0d raw snapshots across wrapped cursors",checkpoint_phase);
    endtask
