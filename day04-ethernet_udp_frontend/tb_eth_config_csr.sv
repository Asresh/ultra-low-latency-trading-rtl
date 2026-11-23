`timescale 1ns/1ps

module tb_eth_config_csr;
    logic clk = 0;
    always #5 clk = !clk;
    logic rst = 1;

    logic cmd_valid = 0, cmd_ready, cmd_write = 0;
    logic [7:0] cmd_address = 0;
    logic [31:0] cmd_value = 0;
    logic [3:0] cmd_strobe = 0;
    logic rsp_valid, rsp_ready = 0;
    logic [1:0] rsp_status;
    logic [31:0] rsp_value;
    logic rule_write_valid, rule_write_ready;
    logic [2:0] rule_write_index;
    logic [255:0] rule_write_data;
    logic rule_commit_valid, rule_commit_ready;
    logic [7:0] rule_epoch;
    logic stats_snapshot, stats_clear_live;
    logic [4:0] stats_index;
    logic [63:0] stats_data;
    logic [31:0] stats_snapshot_id;

    logic [511:0] deltas = 0;
    logic pause_writes = 0, pause_commits = 0;
    logic device_write_ready, device_commit_ready;
    logic match_valid, match_drop;
    logic [2:0] match_queue;
    logic [63:0] model_live [0:31];
    logic [31:0] selected;
    logic [255:0] expected_rule;
    integer pushes = 0, commits = 0, snapshots = 0, clears = 0, pipe_stalls = 0;
    integer cycle = 0;

    eth_config_csr dut (.*);
    eth_rule_table table_under_test (
        .clk, .rst,
        .write_valid(rule_write_valid && !pause_writes), .write_ready(device_write_ready),
        .write_index(rule_write_index), .write_rule(rule_write_data),
        .commit_valid(rule_commit_valid && !pause_commits), .commit_ready(device_commit_ready),
        .active_epoch(rule_epoch), .source_ip(32'h0a0f0409), .destination_ip(32'hef010203),
        .source_port(16'd12000), .destination_port(16'd18000),
        .outer_tci(16'd0), .vlan_depth(2'd0), .match_valid, .match_drop, .match_queue
    );
    eth_counter_bank device_stats (
        .clk, .rst, .deltas, .clear_live(stats_clear_live), .capture(stats_snapshot),
        .read_index(stats_index), .read_value(stats_data), .snapshot_id(stats_snapshot_id)
    );

    assign rule_write_ready = !pause_writes && device_write_ready;
    assign rule_commit_ready = !pause_commits && device_commit_ready;

    always @(posedge clk) begin
        cycle = cycle + 1;
        if (cycle > 10000)
            $fatal(1, "CSR watchdog triggered");
        if (!rst) begin
            if (rule_write_valid && !rule_write_ready) begin
                if (rule_write_data !== expected_rule || rule_write_index != 3'd4)
                    $fatal(1, "stalled rule changed");
                pipe_stalls = pipe_stalls + 1;
            end
            if (rule_write_valid && rule_write_ready)
                pushes = pushes + 1;
            if (rule_commit_valid && rule_commit_ready)
                commits = commits + 1;
            if (stats_snapshot)
                snapshots = snapshots + 1;
            if (stats_clear_live)
                clears = clears + 1;
        end
    end

    task automatic begin_access(input logic wr, input logic [7:0] address,
                                input logic [31:0] data, input logic [3:0] lanes);
        begin
            @(negedge clk);
            cmd_valid = 1;
            cmd_write = wr;
            cmd_address = address;
            cmd_value = data;
            cmd_strobe = lanes;
            do @(posedge clk); while (!cmd_ready);
            @(negedge clk);
            cmd_valid = 0;
        end
    endtask

    task automatic expect_reply(input logic [1:0] code, input logic [31:0] data,
                                 input integer delay_response);
        begin
            while (!rsp_valid) @(negedge clk);
            repeat (delay_response) begin
                if (cmd_ready || !rsp_valid || {rsp_status,rsp_value} !== {code,data})
                    $fatal(1, "CSR response changed during host stall");
                @(negedge clk);
            end
            if ({rsp_status,rsp_value} !== {code,data})
                $fatal(1, "CSR access %02x expected %0d/%h got %0d/%h",
                       cmd_address, code, data, rsp_status, rsp_value);
            rsp_ready = 1;
            @(negedge clk);
            rsp_ready = 0;
        end
    endtask

    task automatic reg_access(input logic wr, input logic [7:0] address,
                              input logic [31:0] value, input logic [3:0] lanes,
                              input logic [1:0] code, input logic [31:0] expected);
        begin
            begin_access(wr, address, value, lanes);
            expect_reply(code, expected, 2);
        end
    endtask

    task automatic write_word(input logic [7:0] address, input logic [31:0] value,
                              input logic [31:0] expected);
        reg_access(1,address,value,4'hf,2'd0,expected);
    endtask

    task automatic read_word(input logic [7:0] address, input logic [31:0] expected);
        reg_access(0,address,32'd0,4'd0,2'd0,expected);
    endtask

    initial begin
        for (integer i=0; i<32; i=i+1)
            model_live[i] = 0;
        expected_rule = '0;
        expected_rule[191] = 1;
        expected_rule[189:187] = 3'd6;
        expected_rule[175:144] = 32'hef010203;
        expected_rule[143:112] = 32'hffffffff;
        expected_rule[111:96] = 16'd18000;
        expected_rule[95:80] = 16'hffff;
        repeat (3) @(negedge clk);
        rst = 0;

        read_word(8'h00, 32'h45554634);
        read_word(8'h04, 32'h00010000);
        read_word(8'h08, 0);
        reg_access(1,8'h00,1,4'hf,2'd2,0);
        reg_access(0,8'h41,0,0,2'd1,0);
        reg_access(0,8'h40,0,0,2'd2,0);
        reg_access(1,8'h40,1,4'b0010,2'd3,0);
        reg_access(1,8'h10,8,4'hf,2'd3,0);
        write_word(8'h10,4,4);
        for (integer i=0; i<8; i=i+1) begin
            selected = expected_rule[32*i +: 32];
            reg_access(1,8'(32+4*i),selected & 32'h00ff00ff,4'b0101,2'd0,
                       selected & 32'h00ff00ff);
            reg_access(1,8'(32+4*i),selected & 32'hff00ff00,4'b1010,2'd0,selected);
            read_word(8'(32+4*i),selected);
        end
        pause_writes = 1;
        begin_access(1,8'h40,1,4'hf);
        repeat (5) @(negedge clk);
        if (rsp_valid || !rule_write_valid || match_valid)
            $fatal(1, "rule became visible before staging handshake");
        pause_writes = 0;
        expect_reply(0,0,4);
        if (match_valid)
            $fatal(1, "staged rule leaked into active bank");
        pause_commits = 1;
        begin_access(1,8'h44,1,4'hf);
        repeat (6) @(negedge clk);
        if (rule_epoch != 0 || !rule_commit_valid || rsp_valid)
            $fatal(1, "commit occurred while paused");
        pause_commits = 0;
        expect_reply(0,0,4);
        if (!match_valid || match_drop || match_queue != 6)
            $fatal(1, "committed rule is not applied");
        read_word(8'h08, 1);

        @(negedge clk);
        for (integer i=0; i<32; i=i+1)
            deltas[16*i +: 16] = 16'(i*43+13);
        repeat (9) @(negedge clk);
        deltas = '0;
        for (integer i=0; i<32; i=i+1)
            model_live[i] = 64'(9*(43*i+13));
        write_word(8'h48,1,0);
        read_word(8'h5c,1);
        for (integer i=0; i<32; i=i+1) begin
            write_word(8'h50,32'(i),32'(i));
            read_word(8'h54,model_live[i][31:0]);
            read_word(8'h58,model_live[i][63:32]);
        end
        write_word(8'h4c,1,0);
        read_word(8'h54,model_live[31][31:0]);
        write_word(8'h48,1,0);
        read_word(8'h54,0);
        read_word(8'h5c,2);
        if (pushes != 1 || commits != 1 || snapshots != 2 || clears != 1 || pipe_stalls < 4)
            $fatal(1, "CSR command counts differ");
        $display("PASS CSR: byte strobes, command stalls, coherent snapshot, 32 indices");
        $finish;
    end
endmodule
