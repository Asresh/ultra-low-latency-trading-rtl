`timescale 1ns/1ps
`default_nettype none

module tb_fixed_point_notional #(parameter integer PRICE_FRAC = 8);
    logic clk = 0;
    always #5 clk = ~clk;
    logic rst = 1;
    logic req_valid = 0, req_sell = 0, req_limit_en = 0, rsp_ready = 0;
    logic [15:0] req_tag = 0, req_fee_bps = 0;
    logic [31:0] req_price = 0, req_qty = 0;
    logic [1:0] req_round = 0;
    logic [63:0] req_limit = 0;
    wire req_ready, rsp_valid, rsp_sell, rsp_inexact;
    wire [15:0] rsp_tag;
    wire [2:0] rsp_status;
    wire [63:0] rsp_gross, rsp_fee, rsp_cash;

    fixed_point_notional #(.PRICE_FRAC(PRICE_FRAC)) dut (.*);

    logic expected_sell, expected_inexact;
    logic [15:0] expected_tag;
    logic [2:0] expected_status;
    logic [63:0] expected_gross, expected_fee, expected_cash;
    integer expected_latency;
    integer cycle = 0, record = 0, stalls = 0, resets = 0, reads, writes, fields;
    integer exact = 0, rounded = 0, errors = 0;
    integer begin_cycle, first_reply;
    logic [31:0] noise = 32'hdf41ef53;
    string source_name, target_name, waveform_path;

    always @(posedge clk) cycle <= cycle + 1;
    always @(negedge clk) noise = {noise[30:0],noise[31] ^ noise[24] ^ noise[8] ^ noise[0]};

    // Compare the complete response, including data in rows with a failed cash limit.
    task automatic check_reply;
        if ({rsp_tag,rsp_status,rsp_sell,rsp_gross,rsp_fee,rsp_cash,rsp_inexact} !==
            {expected_tag,expected_status,expected_sell,expected_gross,
             expected_fee,expected_cash,expected_inexact})
            $fatal(1,"Q%0d row %0d: got code %0d gross=%h fee=%h cash=%h expected %0d/%h/%h/%h",
                   PRICE_FRAC,record,rsp_status,rsp_gross,rsp_fee,rsp_cash,
                   expected_status,expected_gross,expected_fee,expected_cash);
    endtask

    // Abort after a valid request has begun and check that no partial reply survives.
    task automatic reset_busy(input integer after_cycles);
        @(negedge clk);
        req_valid = 1;
        req_tag = 16'hFFDE;
        req_price = 32'h21AB3456;
        req_qty = 9;
        req_fee_bps = 179;
        req_round = 2;
        req_limit_en = 0;
        do @(posedge clk); while (!req_ready);
        @(negedge clk);
        req_valid = 0;
        repeat (after_cycles) @(negedge clk);
        rst = 1;
        repeat (2) @(negedge clk);
        rst = 0;
        repeat (3) begin
            @(posedge clk);
            if (rsp_valid || !req_ready) $fatal(1,"Reset leaked partial arithmetic state");
        end
        resets++;
    endtask

    // Wait for the exact first reply cycle, then apply independent sink backpressure.
    task automatic run_record;
        req_valid = 1;
        rsp_ready = 0;
        do @(posedge clk); while (!req_ready);
        begin_cycle = cycle;
        $fdisplay(writes,"req %0d %h %h %h %h %h %b %b %h",
                  cycle,req_tag,req_price,req_qty,req_fee_bps,req_round,req_sell,req_limit_en,req_limit);
        @(negedge clk);
        req_valid = 0;
        req_tag = ~req_tag;
        req_price = ~req_price;
        req_qty = ~req_qty;
        while (!rsp_valid) begin
            @(posedge clk);
            if (req_ready) $fatal(1,"Arithmetic accepted input before pending computation completed");
            @(negedge clk);
        end
        first_reply = cycle;
        if (first_reply - begin_cycle != expected_latency)
            $fatal(1,"Q%0d row %0d: first reply in %0d cycles expected %0d",
                   PRICE_FRAC,record,first_reply-begin_cycle,expected_latency);
        for (integer blocked = 0; blocked < int'(noise[3:0]);) begin
            if (!rsp_valid) $fatal(1,"Held reply vanished");
            check_reply();
            blocked++;
            stalls++;
            @(negedge clk);
        end
        rsp_ready = 1;
        @(posedge clk);
        if (!rsp_valid || req_ready) $fatal(1,"Bad handshake at response transfer");
        check_reply();
        $fdisplay(writes,"rsp %0d %0d %h %h %b %h %h %h %b",
                  cycle,first_reply,rsp_tag,rsp_status,rsp_sell,rsp_gross,rsp_fee,rsp_cash,rsp_inexact);
        errors += int'(rsp_status != 0);
        exact += int'(!rsp_inexact);
        rounded += int'(rsp_inexact);
        record++;
    endtask

    initial begin
        if ($value$plusargs("waveform=%s",waveform_path)) begin
            $dumpfile(waveform_path);
            $dumpvars(1,tb_fixed_point_notional);
        end
        if (!$value$plusargs("source=%s",source_name) || !$value$plusargs("trace=%s",target_name))
            $fatal(1,"Specify +source= and +trace=");
        reads = $fopen(source_name,"r");
        writes = $fopen(target_name,"w");
        if (!reads || !writes) $fatal(1,"Fixture file open failed");
        repeat (3) @(negedge clk);
        rst = 0;
        reset_busy(0);
        reset_busy(31);
        reset_busy(82);
        while (!$feof(reads)) begin
            @(negedge clk);
            fields = $fscanf(reads,"%h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h",
                req_tag, req_price, req_qty, req_fee_bps, req_round, req_sell, req_limit_en, req_limit,
                expected_tag, expected_status, expected_sell, expected_gross, expected_fee,
                expected_cash, expected_inexact, expected_latency);
            if (fields != 16 && !$feof(reads)) $fatal(1,"Malformed row %0d",record);
            if (fields == 16) run_record();
        end
        if (record == 0 || (!$test$plusargs("small") &&
            (record < 500 || stalls < 30 || errors < 100 || rounded < 100)))
            $fatal(1,"Arithmetic tape did not exercise enough scenarios");
        $fclose(reads);
        $fclose(writes);
        $display("PASS Q%0d: %0d replies (%0d exact, %0d inexact, %0d error), %0d hold cycles, %0d busy resets",
                 PRICE_FRAC,record,exact,rounded,errors,stalls,resets);
        $finish;
    end
    initial begin #6000000; $fatal(1,"Notional regression watchdog"); end
endmodule

`default_nettype wire
