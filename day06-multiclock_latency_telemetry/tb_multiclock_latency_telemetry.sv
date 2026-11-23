`timescale 1ns/1ps

module tb_multiclock_latency_telemetry;
    localparam integer TAG_W = 16;
    localparam integer CLASS_W = 2;
    localparam integer PROBES = 3;
    logic ref_clk = 0;
    always #5 ref_clk = !ref_clk;
    logic [2:0] probe_clk = '0;
    always #3 probe_clk[0] = !probe_clk[0];
    always #7 probe_clk[1] = !probe_clk[1];
    always #11 probe_clk[2] = !probe_clk[2];

    logic power_reset = 0;
    logic pps_async = 0;
    logic pps_ready = 0;
    wire pps_valid;
    wire [63:0] pps_ticks, pps_nanoseconds, now_tick, now_ns;
    wire epoch_exhausted;

    logic [PROBES-1:0] probe_valid = '0, probe_end = '0;
    logic [PROBES*TAG_W-1:0] probe_tags = '0;
    logic [PROBES*CLASS_W-1:0] probe_classes = '0;
    wire [PROBES-1:0] probe_ready;

    logic [11:0] s_awaddr = '0, s_araddr = '0;
    logic s_awvalid = 0, s_arvalid = 0, s_wvalid = 0;
    wire s_awready, s_arready, s_wready;
    logic [31:0] s_wdata = '0;
    logic [3:0] s_wstrb = '0;
    logic s_bready = 0, s_rready = 0;
    wire s_bvalid, s_rvalid;
    wire [1:0] s_bresp, s_rresp;
    wire [31:0] s_rdata;

    logic event_ready = 1, hist_ready = 0, trace_ready = 0;
    wire event_valid, hist_valid, hist_first, hist_last;
    wire trace_valid, trace_first, trace_last;
    wire [319:0] event_data, trace_word;
    wire [127:0] hist_word;

    multiclock_latency_telemetry #(
        .PROBES(PROBES), .FIFO_ADDRESS_W(2), .TABLE_INDEX_W(3),
        .SCAN_LOG(2), .HIST_BINS(12), .TRACE_ADDRESS_W(3)
    ) dut (.*);

    string folder;
    integer event_file, timing_file, hist_file, trace_file;
    integer report_count = 0, trace_frames = 0, hist_frames = 0;
    integer event_stalls = 0, hist_stalls = 0, trace_stalls = 0;
    logic [319:0] events [0:511];
    logic event_hold = 0, hist_hold = 0, trace_hold = 0;
    logic [319:0] event_held;
    logic [321:0] trace_held;
    logic [129:0] hist_held;
    integer hist_words = 0, trace_words = 0;
    integer probe_count [0:2];
    integer axi_reads = 0, axi_writes = 0;

    always @(posedge ref_clk) begin
        if (power_reset) begin
            event_hold = 0;
            hist_hold = 0;
            trace_hold = 0;
        end else begin
            if (event_hold && !dut.flush && (!event_valid || event_data !== event_held))
                $fatal(1, "A report changed under stall before the control fence");
            if (hist_hold && (!hist_valid || {hist_first,hist_last,hist_word} !== hist_held))
                $fatal(1, "Histogram beat changed under stall");
            if (trace_hold && (!trace_valid || {trace_first,trace_last,trace_word} !== trace_held))
                $fatal(1, "Trace beat changed under stall");
            event_hold = event_valid && !event_ready;
            hist_hold = hist_valid && !hist_ready;
            trace_hold = trace_valid && !trace_ready;
            if (event_hold) begin event_held = event_data; event_stalls++; end
            if (hist_hold) begin hist_held = {hist_first,hist_last,hist_word}; hist_stalls++; end
            if (trace_hold) begin trace_held = {trace_first,trace_last,trace_word}; trace_stalls++; end

            if (event_valid && event_ready) begin
                if (report_count >= 512) $fatal(1, "Unexpected excess reports");
                events[report_count] = event_data;
                $fwrite(event_file, "%080h\n", event_data);
                $fwrite(timing_file, "%016h\n", now_tick);
                report_count++;
            end
            if (hist_valid && hist_ready) begin
                if (hist_first !== (hist_words % 57 == 0) ||
                    hist_last !== (hist_words % 57 == 56))
                    $fatal(1, "Snapshot framing error");
                $fwrite(hist_file, "%032h\n", hist_word);
                hist_words++;
                if (hist_last) hist_frames++;
            end
            if (trace_valid && trace_ready) begin
                if (trace_first !== (trace_words == 0))
                    $fatal(1, "Trace framing error");
                $fwrite(trace_file, "%080h\n", trace_word);
                trace_words++;
                if (trace_last) trace_frames++;
            end
        end
    end

    task automatic drive_address(input logic [11:0] addr, input integer delay);
        begin
            repeat (delay) @(negedge ref_clk);
            s_awaddr = addr;
            s_awvalid = 1;
            do @(posedge ref_clk); while (!s_awready);
            @(negedge ref_clk);
            s_awvalid = 0;
        end
    endtask

    task automatic drive_write(input logic [31:0] value, input logic [3:0] mask, input integer delay);
        begin
            repeat (delay) @(negedge ref_clk);
            s_wdata = value;
            s_wstrb = mask;
            s_wvalid = 1;
            do @(posedge ref_clk); while (!s_wready);
            @(negedge ref_clk);
            s_wvalid = 0;
        end
    endtask

    task automatic write_register(
        input logic [11:0] addr,
        input logic [31:0] data,
        input logic [3:0] mask,
        input logic [1:0] status,
        input integer skew
    );
        begin
            @(negedge ref_clk);
            fork
                drive_address(addr, skew > 0 ? skew : 0);
                drive_write(data, mask, skew < 0 ? -skew : 0);
            join
            while (!s_bvalid) @(negedge ref_clk);
            repeat (3) begin
                if (!s_bvalid || s_bresp !== status)
                    $fatal(1, "Write status for %h returned %0d instead of %0d", addr, s_bresp, status);
                @(negedge ref_clk);
            end
            s_bready = 1;
            @(negedge ref_clk);
            s_bready = 0;
            axi_writes++;
        end
    endtask

    task automatic read_register(
        input logic [11:0] addr,
        input logic [1:0] status,
        output logic [31:0] value
    );
        begin
            @(negedge ref_clk);
            s_araddr = addr;
            s_arvalid = 1;
            do @(posedge ref_clk); while (!s_arready);
            @(negedge ref_clk);
            s_arvalid = 0;
            while (!s_rvalid) @(negedge ref_clk);
            value = s_rdata;
            repeat (3) begin
                if (!s_rvalid || s_rresp !== status || s_rdata !== value)
                    $fatal(1, "Read reply changed or gave the wrong status for %h", addr);
                @(negedge ref_clk);
            end
            s_rready = 1;
            @(negedge ref_clk);
            s_rready = 0;
            axi_reads++;
        end
    endtask

    task automatic observe(
        input integer lane, input logic [15:0] tag,
        input logic [1:0] event_class, input logic ending
    );
        begin
            @(negedge probe_clk[lane]);
            probe_valid[lane] = 1;
            probe_tags[lane*TAG_W+:TAG_W] = tag;
            probe_classes[lane*CLASS_W+:CLASS_W] = event_class;
            probe_end[lane] = ending;
            do @(posedge probe_clk[lane]); while (!probe_ready[lane]);
            probe_count[lane]++;
            @(negedge probe_clk[lane]);
            probe_valid[lane] = 0;
        end
    endtask

    task automatic expect_report(
        input integer index, input logic [2:0] code,
        input logic [15:0] tag, input logic [7:0] event_class
    );
        begin
            wait (report_count > index);
            @(negedge ref_clk);
            if (events[index][319:293] !== {code,event_class,tag})
                $fatal(1, "Report %0d: got %h expected {%0d,%0d,%0d}",
                       index, events[index], code, event_class, tag);
            if (events[index][276:256] != 0)
                $fatal(1, "Report reserved bits are nonzero");
            if (code == 1) begin
                if (events[index][191:128] - events[index][255:192] != events[index][127:64] ||
                    events[index][127:64] == 0)
                    $fatal(1, "Wrong timestamp or zero interval on a directed trade");
            end
        end
    endtask

    task automatic pulse;
        begin
            @(negedge ref_clk);
            pps_async = 1;
            repeat (5) @(negedge ref_clk);
            pps_async = 0;
            repeat (5) @(negedge ref_clk);
        end
    endtask

    task automatic drain_histogram;
        begin
            while (!hist_valid) @(negedge ref_clk);
            repeat (7) @(negedge ref_clk);
            for (integer slot = 0; slot < 260 && hist_frames == 0; slot++) begin
                hist_ready = slot % 5 != 1;
                @(negedge ref_clk);
            end
            hist_ready = 0;
            if (hist_frames != 1 || hist_words != 57)
                $fatal(1, "Histogram was incomplete");
        end
    endtask

    task automatic drain_trace;
        begin
            while (!trace_valid) @(negedge ref_clk);
            repeat (10) @(negedge ref_clk);
            for (integer slot = 0; slot < 80 && trace_frames == 0; slot++) begin
                trace_ready = slot % 3 != 0;
                @(negedge ref_clk);
            end
            trace_ready = 0;
            if (trace_frames != 1 || trace_words != 5)
                $fatal(1, "Expected four records around the trigger");
        end
    endtask

    initial begin : scenario
        logic [31:0] value;
        logic [63:0] ns_before;
        logic [63:0] pps_before;
        integer start_index;
        if (!$value$plusargs("out=%s", folder)) folder = "build";
        event_file = $fopen({folder, "/events.hex"}, "w");
        timing_file = $fopen({folder, "/transfers.hex"}, "w");
        hist_file = $fopen({folder, "/histogram.hex"}, "w");
        trace_file = $fopen({folder, "/trace.hex"}, "w");
        if (!event_file || !timing_file || !hist_file || !trace_file)
            $fatal(1, "Cannot write capture files");
        for (integer lane = 0; lane < PROBES; lane++) probe_count[lane] = 0;
        #1;
        power_reset = 1;
        repeat (10) @(negedge ref_clk);
        power_reset = 0;
        wait (&probe_ready);
        read_register('h000, 2'b00, value);
        if (value != 32'h4c415431) $fatal(1, "Device ID incorrect");
        read_register('h003, 2'b11, value);
        write_register('h000, 42, 4'hf, 2'b10, 3);
        write_register('h010, 6, 4'b0001, 2'b00, -2);
        read_register('h010, 2'b00, value);
        if (value != 6) $fatal(1, "Byte strobes incorrectly merged");
        write_register('h014, 32'h80000000, 4'b1000, 2'b00, 0);
        write_register('h008, 'h10, 4'hf, 2'b00, 2);
        ns_before = now_ns;
        repeat (10) @(negedge ref_clk);
        if (now_ns - ns_before != 65) $fatal(1, "6.5 ns fixed-point step incorrect");
        pulse();
        if (!pps_valid || pps_nanoseconds >= now_ns) $fatal(1, "PPS capture missing");
        pps_before = pps_ticks;
        pulse();
        if (!pps_valid || pps_ticks != pps_before) $fatal(1, "PPS replaced held observation");
        read_register('h04c, 2'b00, value);
        if (value != 1) $fatal(1, "PPS overrun not counted");
        pps_ready = 1;
        @(negedge ref_clk);
        pps_ready = 0;
        if (pps_valid) $fatal(1, "PPS observation not consumed");

        observe(2, 9, 0, 0);
        expect_report(0, 6, 9, 0);
        write_register('h028, 32'h00010204, 4'hf, 2'b00, 3);
        write_register('h008, 'h41, 4'hf, 2'b00, -3);
        observe(0, 55, 1, 0);
        expect_report(1, 0, 55, 1);
        observe(0, 55, 1, 0);
        expect_report(2, 2, 55, 1);
        observe(1, 55, 1, 1);
        expect_report(3, 1, 55, 1);
        if ({events[3][292:285],events[3][284:277]} != {8'd0,8'd1})
            $fatal(1, "Wrong probe metadata after pairing");
        observe(1, 76, 2, 1);
        expect_report(4, 0, 76, 2);
        drain_trace();
        observe(2, 76, 2, 0);
        expect_report(5, 4, 76, 2);

        // Stall the primary report writer and retain three old-epoch packets
        // across a flush to prove they cannot repopulate the table.
        event_ready = 0;
        observe(0, 120, 0, 0);
        wait (event_valid);
        observe(1, 121, 0, 0);
        observe(1, 122, 0, 0);
        observe(1, 123, 0, 1);
        start_index = report_count;
        write_register('h008, 'h09, 4'hf, 2'b00, 0);
        repeat (3) @(negedge ref_clk);
        event_ready = 1;
        expect_report(start_index, 6, 121, 0);
        expect_report(start_index+1, 6, 122, 0);
        expect_report(start_index+2, 6, 123, 0);
        observe(1, 124, 0, 0);
        expect_report(start_index+3, 0, 124, 0);
        observe(2, 124, 0, 1);
        expect_report(start_index+4, 1, 124, 0);

        write_register('h008, 'h03, 4'hf, 2'b00, 1);
        drain_histogram();
        read_register('h084, 2'b00, value);
        if (value != 2) $fatal(1, "MMIO match count disagrees with trace");
        write_register('h008, 'h85, 4'hf, 2'b00, 0);
        read_register('h030, 2'b00, value);
        if (value == 0 || value >= now_tick[31:0])
            $fatal(1, "Latched clock register is not a past snapshot");
        read_register('h084, 2'b00, value);
        if (value != 0) $fatal(1, "Clear status counters did not take effect");
        if (event_stalls == 0 || trace_stalls == 0 || hist_stalls == 0)
            $fatal(1, "Expected stall coverage on every egress");

        $display("PASS telemetry end-to-end: %0d replies, %0d trace and %0d histogram frames, %0d reads/%0d writes",
                 report_count, trace_frames, hist_frames, axi_reads, axi_writes);
        $display("Probe accepts %0d/%0d/%0d; held event/trace/hist %0d/%0d/%0d",
                 probe_count[0],probe_count[1],probe_count[2],event_stalls,trace_stalls,hist_stalls);
        $fclose(event_file);
        $fclose(timing_file);
        $fclose(hist_file);
        $fclose(trace_file);
        $finish;
    end
    initial begin #180000; $fatal(1, "Multiclock telemetry timeout"); end
endmodule
