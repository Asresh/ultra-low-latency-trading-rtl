`timescale 1ns/1ps
`default_nettype none

module tb_dma_axi64 #(
    parameter integer MAX_BURST = 5
);
    logic clk = 0;
    always #5 clk = ~clk;
    logic reset;
    logic rd_cmd_valid, rd_ready, wr_cmd_valid, wr_valid;
    logic [63:0] rd_cmd_addr, wr_cmd_addr, wr_data;
    logic [31:0] rd_cmd_bytes, wr_cmd_bytes;
    wire rd_cmd_ready, wr_cmd_ready, rd_valid, rd_last, rd_done, rd_fault, rd_protocol;
    wire wr_ready, wr_done, wr_fault;
    wire [63:0] rd_data;
    wire [7:0] rd_keep;
    wire arvalid, rready, awvalid, wvalid, wlast, bready;
    wire [63:0] araddr, awaddr, wdata;
    wire [7:0] arlen, awlen, wstrb;
    wire [2:0] arsize, awsize;
    wire [1:0] arburst, awburst;
    logic arready, rvalid, rlast, awready, wready, bvalid;
    logic [1:0] rresp, bresp;
    logic [63:0] rdata;

    dma_axi64 #(.MAX_BURST(MAX_BURST)) dut (.*);

    localparam integer OKAY = 0;
    localparam integer FAULT_R = 1;
    localparam integer EARLY_LAST = 2;
    localparam integer ABSENT_LAST = 3;
    localparam integer R_AND_LAST = 4;
    localparam integer FAULT_B = 1;
    logic [31:0] rng = 32'h0d0a_7749;

    function automatic [31:0] random_word(input logic [31:0] prior);
        logic [31:0] tmp;
        begin
            tmp = prior ^ (prior << 13);
            tmp = tmp ^ (tmp >> 17);
            random_word = tmp ^ (tmp << 5);
        end
    endfunction

    function automatic [63:0] data_at(input logic [63:0] address, input logic direction);
        logic [63:0] x;
        begin
            x = address ^ (direction ? 64'h8171_7059_734c_a5b0 : 64'h5e92_079a_d2d1_e480);
            data_at = (x << 17) ^ (x >> 11) ^ ~address;
        end
    endfunction

    function automatic integer choose_burst(input logic [63:0] address, input integer bytes);
        integer left_on_page, planned;
        begin
            left_on_page = (4096 - int'(address[11:0])) / 8;
            planned = (bytes + 7) / 8;
            if (planned > MAX_BURST)
                planned = MAX_BURST;
            if (planned > left_on_page)
                planned = left_on_page;
            choose_burst = planned;
        end
    endfunction

    function automatic [7:0] mask_for(input integer remaining);
        integer bytes;
        begin
            bytes = remaining < 8 ? remaining : 8;
            if (bytes < 0) bytes = 0;
            mask_for = 8'((1 << bytes) - 1);
        end
    endfunction

    integer cases_checked = 0, ar_count = 0, aw_count = 0, r_count = 0, w_count = 0;
    integer page_splits = 0, cap_splits = 0, partial_masks = 0;
    integer blocked_read = 0, blocked_write = 0, blocked_ar = 0, blocked_aw = 0;
    integer overlap_beats = 0, b_errors = 0, r_errors = 0, rlast_errors = 0;
    bit ar_held = 0, aw_held = 0, w_held = 0, rd_held = 0;
    logic [76:0] held_ar, held_aw;
    logic [72:0] held_w, held_rd;

    always @(posedge clk) begin
        if (reset) begin
            if (arvalid || awvalid || rready || bready || wr_ready || rd_valid)
                $fatal(1, "AXI handshake escaped reset");
            ar_held = 0;
            aw_held = 0;
            w_held = 0;
            rd_held = 0;
        end else begin
            if (ar_held && (!arvalid || {araddr, arlen, arsize, arburst} !== held_ar))
                $fatal(1, "AR mutated under stall");
            if (aw_held && (!awvalid || {awaddr, awlen, awsize, awburst} !== held_aw))
                $fatal(1, "AW mutated under stall");
            if (w_held && (!wvalid || {wdata, wstrb, wlast} !== held_w))
                $fatal(1, "W mutated under stall");
            if (rd_held && (!rd_valid || {rd_data, rd_keep, rd_last} !== held_rd))
                $fatal(1, "read result mutated under backpressure");
            ar_held = arvalid && !arready;
            aw_held = awvalid && !awready;
            w_held = wvalid && !wready;
            rd_held = rd_valid && !rd_ready;
            held_ar = {araddr, arlen, arsize, arburst};
            held_aw = {awaddr, awlen, awsize, awburst};
            held_w = {wdata, wstrb, wlast};
            held_rd = {rd_data, rd_keep, rd_last};
            if (ar_held) blocked_ar++;
            if (aw_held) blocked_aw++;
            if (w_held) blocked_write++;
            if (rd_held) blocked_read++;
            if (rvalid && rready && wvalid && wready)
                overlap_beats++;
        end
    end

    task automatic validate_address(
        input logic [63:0] actual_addr,
        input logic [7:0] actual_len,
        input logic [2:0] actual_size,
        input logic [1:0] actual_burst,
        input logic [63:0] expected_addr,
        input integer expected_beats,
        input integer expected_bytes,
        input string direction
    );
        integer offset;
        begin
            offset = int'(actual_addr[11:0]);
            if (actual_addr !== expected_addr || int'(actual_len) + 1 != expected_beats ||
                actual_size !== 3'd3 || actual_burst !== 2'b01 ||
                offset + 8 * expected_beats > 4096)
                $fatal(1, "%s plan addr=%h len=%0d size=%0d burst=%0d expected addr=%h beats=%0d",
                       direction, actual_addr, actual_len, actual_size, actual_burst,
                       expected_addr, expected_beats);
            if (offset + 8 * expected_beats == 4096 && expected_bytes > 8 * expected_beats)
                page_splits++;
            if (expected_beats == MAX_BURST && expected_bytes > 8 * expected_beats)
                cap_splits++;
        end
    endtask

    task automatic run_case(
        input logic [63:0] start_r, start_w,
        input integer bytes_r, bytes_w, mode_r, mode_w
    );
        logic [63:0] expect_r, expect_w;
        integer left_r, left_w;
        integer burst_r, burst_w, chosen, cycle;
        integer rburst_index, wburst_index;
        bit read_issued, write_issued, read_finished, write_finished;
        bit expected_r_fault, expected_w_fault, expected_protocol;
        bit expected_r_done, expected_w_done, wait_response;
        bit prior_r, prior_w, prior_b, malformed;
        begin
            expect_r = start_r;
            expect_w = start_w;
            left_r = bytes_r;
            left_w = bytes_w;
            burst_r = 0;
            burst_w = 0;
            cycle = 0;
            rburst_index = 0;
            wburst_index = 0;
            read_issued = 0;
            write_issued = 0;
            read_finished = 0;
            write_finished = 0;
            wait_response = 0;
            expected_r_fault = rd_fault;
            expected_w_fault = wr_fault;
            expected_protocol = rd_protocol;
            prior_r = 0;
            prior_w = 0;
            prior_b = 0;
            while (!read_finished || !write_finished) begin
                @(negedge clk);
                rng = random_word(rng);
                cycle++;
                if (cycle > 2000)
                    $fatal(1, "AXI case %0d failed to make progress R=%0d W=%0d", cases_checked, left_r, left_w);
                rd_cmd_valid = !read_issued;
                rd_cmd_addr = start_r;
                rd_cmd_bytes = 32'(bytes_r);
                wr_cmd_valid = !write_issued;
                wr_cmd_addr = start_w;
                wr_cmd_bytes = 32'(bytes_w);
                arready = rng[2:0] < 5;
                awready = rng[5:3] < 5;
                rd_ready = rng[8:6] < 5;
                wready = rng[11:9] < 5;

                if (!prior_r) begin
                    rvalid = burst_r > 0 && rng[13:12] != 0;
                    rdata = data_at(expect_r, 0);
                    rlast = burst_r == 1;
                    rresp = 0;
                    if (rburst_index == 1 && rvalid) begin
                        if (mode_r == FAULT_R || mode_r == R_AND_LAST) rresp = 2'b10;
                        if ((mode_r == EARLY_LAST || mode_r == R_AND_LAST) && burst_r > 1)
                            rlast = 1;
                        if (mode_r == ABSENT_LAST && burst_r == 1) rlast = 0;
                    end
                end
                if (!prior_w) begin
                    wr_valid = burst_w > 0 && rng[16:14] < 6;
                    wr_data = data_at(expect_w, 1);
                end
                if (!prior_b) begin
                    bvalid = wait_response && rng[19:17] < 5;
                    bresp = mode_w == FAULT_B && wburst_index == 1 ? 2'b11 : 2'b00;
                end

                @(posedge clk);
                expected_r_done = 0;
                expected_w_done = 0;
                prior_r = rvalid && !rready;
                prior_w = wr_valid && !wr_ready;
                prior_b = bvalid && !bready;
                if (rd_cmd_valid && rd_cmd_ready) begin
                    read_issued = 1;
                    expected_r_fault = bytes_r == 0 || start_r[2:0] != 0;
                    expected_protocol = 0;
                    expected_r_done = expected_r_fault;
                end
                if (wr_cmd_valid && wr_cmd_ready) begin
                    write_issued = 1;
                    expected_w_fault = bytes_w == 0 || start_w[2:0] != 0;
                    expected_w_done = expected_w_fault;
                end
                if (arvalid && arready) begin
                    if (!read_issued || read_finished || burst_r != 0)
                        $fatal(1, "unexpected read-address transaction");
                    chosen = choose_burst(expect_r, left_r);
                    validate_address(araddr, arlen, arsize, arburst, expect_r,
                                     chosen, left_r, "read");
                    burst_r = chosen;
                    rburst_index++;
                    ar_count++;
                end
                if (awvalid && awready) begin
                    if (!write_issued || write_finished || burst_w != 0 || wait_response)
                        $fatal(1, "unexpected write-address transaction");
                    chosen = choose_burst(expect_w, left_w);
                    validate_address(awaddr, awlen, awsize, awburst, expect_w,
                                     chosen, left_w, "write");
                    burst_w = chosen;
                    wburst_index++;
                    aw_count++;
                end
                if ((rvalid && rready) !== (rd_valid && rd_ready))
                    $fatal(1, "upstream and downstream R handshakes diverged");
                if ((wvalid && wready) !== (wr_valid && wr_ready))
                    $fatal(1, "upstream and downstream W handshakes diverged");
                if (rvalid && rready) begin
                    if (burst_r <= 0 || rd_data !== data_at(expect_r, 0) ||
                        rd_keep !== mask_for(left_r) || rd_last !== (left_r <= 8))
                        $fatal(1, "read data/mask/request terminal mismatch");
                    if (rd_keep != 8'hff) partial_masks++;
                    r_count++;
                    if (rresp != 0) begin
                        expected_r_fault = 1;
                        r_errors++;
                    end
                    malformed = rlast != (burst_r == 1);
                    if (malformed) begin
                        expected_protocol = 1;
                        rlast_errors++;
                        burst_r = 0;
                        expected_r_done = 1;
                    end else begin
                        burst_r--;
                        expect_r += 8;
                        left_r -= 8;
                        if (left_r <= 0) expected_r_done = 1;
                    end
                end
                if (wvalid && wready) begin
                    if (burst_w <= 0 || wdata !== data_at(expect_w, 1) ||
                        wstrb !== mask_for(left_w) || wlast !== (burst_w == 1))
                        $fatal(1, "write data/WSTRB/WLAST mismatch");
                    if (wstrb != 8'hff) partial_masks++;
                    w_count++;
                    burst_w--;
                    expect_w += 8;
                    left_w -= 8;
                    if (burst_w == 0) wait_response = 1;
                end
                if (bvalid && bready) begin
                    if (!wait_response) $fatal(1, "write response accepted before final beat");
                    wait_response = 0;
                    if (bresp != 0) begin
                        expected_w_fault = 1;
                        expected_w_done = 1;
                        b_errors++;
                    end else if (left_w <= 0) expected_w_done = 1;
                end
                #1;
                if (rd_done !== expected_r_done || rd_fault !== expected_r_fault ||
                    rd_protocol !== expected_protocol || wr_done !== expected_w_done ||
                    wr_fault !== expected_w_fault)
                    $fatal(1, "completion flags case %0d cycle %0d input modes R%0d W%0d", cases_checked, cycle, mode_r, mode_w);
                if (rd_done) read_finished = 1;
                if (wr_done) write_finished = 1;
            end
            @(negedge clk);
            rd_cmd_valid = 0;
            wr_cmd_valid = 0;
            rvalid = 0;
            wr_valid = 0;
            bvalid = 0;
            repeat (3) begin
                @(posedge clk);
                #1;
                if (!rd_cmd_ready || !wr_cmd_ready || rd_done || wr_done || arvalid || awvalid)
                    $fatal(1, "unexpected burst or repeated completion after descriptor retire");
            end
            cases_checked++;
        end
    endtask

    logic [63:0] varied_read_addr, varied_write_addr;
    integer chosen_read, chosen_write, variant_r, variant_w;
    initial begin
        reset = 1;
        rd_cmd_valid = 0;
        wr_cmd_valid = 0;
        rd_ready = 0;
        arready = 0;
        awready = 0;
        wready = 0;
        rvalid = 0;
        bvalid = 0;
        wr_valid = 0;
        repeat (3) @(negedge clk);
        reset = 0;
        run_case(64'h1ff8, 64'hbfe8, 347, 198, OKAY, OKAY);
        run_case(64'h0, 64'h20, 0, 0, OKAY, OKAY);
        run_case(64'h127, 64'h22a, 9, 18, OKAY, OKAY);
        run_case(64'h1234_5678_9abc_d000, 64'h8210_0000_fedc_1000, 79, 112, FAULT_R, FAULT_B);
        run_case(64'h8000, 64'h9000, 81, 21, EARLY_LAST, OKAY);
        run_case(64'h9ff8, 64'haff8, 43, 17, ABSENT_LAST, OKAY);
        run_case(64'hb000, 64'hd000, 32, 77, R_AND_LAST, OKAY);
        for (integer i = 0; i < 256; i = i + 1) begin
            rng = random_word(rng);
            chosen_read = 1 + int'(rng[8:0]);
            chosen_write = 1 + int'(rng[16:9]);
            varied_read_addr = {32'h9999_2001, rng[31:12], rng[8:0], 3'b000};
            varied_write_addr = {32'ha9fc_d478, rng[31:12], rng[17:9], 3'b000};
            variant_r = i % 9 == 2 ? FAULT_R :
                        i % 9 == 4 ? ABSENT_LAST :
                        i % 9 == 6 ? EARLY_LAST : OKAY;
            variant_w = i % 11 == 4 ? FAULT_B : OKAY;
            run_case(varied_read_addr, varied_write_addr, chosen_read, chosen_write, variant_r, variant_w);
        end
        if (page_splits < 5 || cap_splits < 40 || partial_masks < 100 ||
            !blocked_read || !blocked_write || !blocked_ar || !blocked_aw ||
            overlap_beats < 100 || !b_errors || !r_errors || !rlast_errors)
            $fatal(1, "AXI coverage did not hit critical queues/errors");
        $display("PASS AXI64 MAX_BURST=%0d: %0d concurrent cases, %0d AR/%0d AW, %0d R/%0d W, %0d page/%0d cap splits",
                 MAX_BURST, cases_checked, ar_count, aw_count, r_count, w_count, page_splits, cap_splits);
        $display("Partial strobe/keep=%0d overlapping=%0d RRESP=%0d BRESP=%0d malformed-last=%0d",
                 partial_masks, overlap_beats, r_errors, b_errors, rlast_errors);
        $finish;
    end

    initial begin
        #5000000;
        $fatal(1, "AXI regression watchdog");
    end
endmodule

`default_nettype wire
