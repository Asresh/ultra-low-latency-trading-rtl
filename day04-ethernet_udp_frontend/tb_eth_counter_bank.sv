`timescale 1ns/1ps

module tb_eth_counter_bank;
    localparam integer COUNTERS = 32, BITS = 18;
    localparam integer MAXIMUM = (1 << BITS)-1;
    logic clk = 0;
    always #5 clk = !clk;
    logic rst = 1;
    logic [511:0] deltas = 0;
    logic clear_live = 0, capture = 0;
    logic [4:0] read_index = 0;
    logic [63:0] read_value;
    logic [31:0] snapshot_id;

    eth_counter_bank #(.ENTRIES(COUNTERS),.COUNTER_BITS(BITS)) dut (.*);

    integer live [0:COUNTERS-1], saved [0:COUNTERS-1];
    integer snaps = 0, saturated = 0, cycles = 0;
    logic [31:0] random_state = 32'h7389aebf;

    // Step the fixed-seed pseudo-random generator used for repeatable test patterns.
    function automatic logic [31:0] random_next(input logic [31:0] value);
        logic [31:0] x;
        begin
            x = value ^ (value << 13);
            x = x ^ (x >> 17);
            random_next = x ^ (x << 5);
        end
    endfunction

    always @(posedge clk) begin
        cycles = cycles + 1;
        if (rst) begin
            snaps = 0;
            for (integer i=0; i<COUNTERS; i=i+1) begin
                live[i] = 0;
                saved[i] = 0;
            end
        end else begin
            if (capture)
                snaps = snaps + 1;
            for (integer i=0; i<COUNTERS; i=i+1) begin
                if (capture)
                    saved[i] = live[i];
                if (clear_live)
                    live[i] = 0;
                else
                    live[i] = live[i]+int'(deltas[16*i +: 16]);
                if (live[i] > MAXIMUM) begin
                    saturated = saturated + 1;
                    live[i] = MAXIMUM;
                end
            end
        end
    end

    // Read every captured Ethernet counter and compare it with the reference counters.
    task automatic read_all;
        begin
            for (integer i=0; i<COUNTERS; i=i+1) begin
                read_index = 5'(i);
                #1;
                if (read_value !== 64'(saved[i]) || snapshot_id !== 32'(snaps))
                    $fatal(1,"snapshot mismatch cycle %0d index %0d",cycles,i);
            end
            @(negedge clk);
        end
    endtask

    initial begin
        repeat (3) @(negedge clk);
        rst = 0;
        for (integer cycle=0; cycle<2000; cycle=cycle+1) begin
            @(negedge clk);
            random_state = random_next(random_state);
            read_index = random_state[4:0];
            capture = cycle % 13 == 0;
            clear_live = cycle % 97 == 0;
            for (integer i=0; i<COUNTERS; i=i+1) begin
                random_state = random_next(random_state);
                deltas[16*i +: 16] = random_state[15:0];
            end
            @(posedge clk);
            #1;
            if (read_value !== 64'(saved[read_index]) || snapshot_id !== 32'(snaps))
                $fatal(1,"random snapshot mismatch at cycle %0d",cycle);
        end
        @(negedge clk);
        capture = 1;
        clear_live = 1;
        deltas = 0;
        @(negedge clk);
        capture = 0;
        clear_live = 0;
        read_all();
        capture = 1;
        @(negedge clk);
        capture = 0;
        read_all();
        if (saturated < 1000 || snaps < 100)
            $fatal(1,"saturation/snapshot coverage missed");
        $display("PASS counters: %0d saturations, %0d coherent snapshots",saturated,snaps);
        $finish;
    end
endmodule
