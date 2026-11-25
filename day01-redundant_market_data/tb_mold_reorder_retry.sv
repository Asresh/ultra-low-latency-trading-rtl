`timescale 1ns/1ps
`default_nettype none

module tb_mold_reorder_retry;
    localparam integer GAP = 4;
    localparam logic [2:0] DATA = 0, HEARTBEAT = 1;
    localparam logic [63:0] WRAP_HEAD = 64'hffff_ffff_ffff_fffe;

    logic clk = 0, clear = 1, session_active = 1;
    logic [63:0] seed_sequence = WRAP_HEAD;
    logic s_valid = 0, m_ready = 1, request_ready = 1;
    logic [2:0] s_kind = 0;
    logic [63:0] s_sequence = '0;
    logic [15:0] s_length = 1;
    logic [7:0] s_payload = '0;
    wire s_ready, m_valid, request_valid, gap_pending, session_end_seen;
    wire [63:0] m_sequence, request_sequence, expected_sequence, high_water;
    wire [15:0] m_length, request_count;
    wire [7:0] m_payload;
    wire [5:0] occupancy;
    wire hit_store, hit_duplicate, hit_conflict, hit_stale, hit_far;

    always #5 clk = !clk;
    mold_reorder #(
        .MESSAGE_BYTES(1), .WINDOW(4), .GAP_TICKS(GAP), .REQUEST_MAX(3),
        .MAX_REQUESTS_PER_HEAD(4), .RETRY_BACKOFF_SHIFT(2)
    ) dut (.*);

    integer cycle = 0, request_total = 0, total_released = 0;
    integer at_cycle [0:15];
    logic [63:0] requested [0:15];
    logic [15:0] amounts [0:15];
    logic previously_held = 0;
    logic [79:0] held;

    always @(posedge clk) begin
        cycle++;
        if (clear) begin
            previously_held = 0;
        end else begin
            if (previously_held && (!request_valid || {request_sequence,request_count} !== held))
                $fatal(1,"reorder changed a held recovery request");
            previously_held = request_valid && !request_ready;
            held = {request_sequence,request_count};
            if (request_valid && request_ready) begin
                if (request_total >= 16)
                    $fatal(1,"unexpected unbounded retries");
                at_cycle[request_total] = cycle;
                requested[request_total] = request_sequence;
                amounts[request_total] = request_count;
                request_total++;
            end
            if (m_valid && m_ready)
                total_released++;
        end
    end

    task automatic seed(input logic [63:0] value);
        @(negedge clk);
        clear = 1;
        seed_sequence = value;
        @(negedge clk);
        clear = 0;
    endtask

    task automatic offer(input logic [2:0] kind, input logic [63:0] sequence_value);
        @(negedge clk);
        s_valid = 1;
        s_kind = kind;
        s_sequence = sequence_value;
        s_payload = sequence_value[7:0];
        do @(posedge clk); while (!s_ready);
        @(negedge clk);
        s_valid = 0;
    endtask

    task automatic expect_request(
        input integer index, delay_cycles, from_cycle,
        input logic [63:0] sequence_value,
        input logic [15:0] count
    );
        while (request_total <= index)
            @(negedge clk);
        if (requested[index] !== sequence_value || amounts[index] !== count)
            $fatal(1,"request %0d got head %h count %0d expected %h/%0d",
                   index,requested[index],amounts[index],sequence_value,count);
        if (at_cycle[index] - from_cycle != delay_cycles)
            $fatal(1,"request %0d delay=%0d expected %0d",
                   index,at_cycle[index]-from_cycle,delay_cycles);
    endtask

    task automatic check_four(
        input integer first, gap_observed_cycle,
        input logic [63:0] sequence_value, input logic [15:0] count
    );
        expect_request(first, GAP+1, gap_observed_cycle, sequence_value, count);
        expect_request(first+1, 2*GAP+1, at_cycle[first], sequence_value, count);
        expect_request(first+2, 4*GAP+1, at_cycle[first+1], sequence_value, count);
        expect_request(first+3, 4*GAP+1, at_cycle[first+2], sequence_value, count);
        repeat (8*GAP) @(negedge clk);
        if (request_total != first+4 || !gap_pending || expected_sequence !== sequence_value)
            $fatal(1,"head no longer obeys its finite request budget");
    endtask

    initial begin : wave_capture
        string path;
        if ($value$plusargs("waveform=%s",path)) begin
            $dumpfile(path);
            $dumpvars(1,tb_mold_reorder_retry);
            $dumpvars(1,tb_mold_reorder_retry.dut.retry_policy);
        end
    end

    initial begin
        seed(WRAP_HEAD);
        offer(HEARTBEAT, 2);
        check_four(0,cycle,WRAP_HEAD,3);
        offer(DATA, WRAP_HEAD);
        @(negedge clk);
        if (expected_sequence !== WRAP_HEAD+1 || total_released != 1)
            $fatal(1,"passive repair did not advance the expected head");
        check_four(4,cycle,WRAP_HEAD+1,3);

        seed(42);
        request_ready = 0;
        offer(HEARTBEAT,45);
        while (!request_valid) @(negedge clk);
        repeat (20) @(negedge clk);
        offer(DATA,42);
        @(negedge clk);
        if (expected_sequence !== 43 || request_sequence !== 42 || request_total != 8)
            $fatal(1,"held request should identify the previous head");
        request_ready = 1;
        @(negedge clk);
        if (request_total != 9 || requested[8] !== 42)
            $fatal(1,"held old request did not drain once");
        check_four(9,cycle,43,2);

        seed(101);
        offer(HEARTBEAT,102);
        repeat (GAP-2) @(negedge clk);
        offer(DATA,101);
        repeat (2*GAP) @(negedge clk);
        if (request_total != 13 || gap_pending || expected_sequence !== 102 || total_released != 3)
            $fatal(1,"repair at the timeout edge generated a spurious request");
        $display("PASS retry scheduler: %0d transfers with capped cadence, %0d passive repairs",
                 request_total,total_released);
        $finish;
    end

    initial begin
        #12000;
        $fatal(1,"retry scheduler timed out");
    end
endmodule

`default_nettype wire
