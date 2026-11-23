`timescale 1ns/1ps

module eth_rule_table (
    input  logic         clk,
    input  logic         rst,
    input  logic         write_valid,
    output logic         write_ready,
    input  logic [2:0]   write_index,
    input  logic [255:0] write_rule,
    input  logic         commit_valid,
    output logic         commit_ready,
    output logic [7:0]   active_epoch,

    input  logic [31:0]  source_ip,
    input  logic [31:0]  destination_ip,
    input  logic [15:0]  source_port,
    input  logic [15:0]  destination_port,
    input  logic [15:0]  outer_tci,
    input  logic [1:0]   vlan_depth,
    output logic         match_valid,
    output logic         match_drop,
    output logic [2:0]   match_queue
);
    logic [255:0] bank0 [0:7];
    logic [255:0] bank1 [0:7];
    logic [7:0] valid0, valid1;
    logic active_bank;
    logic [31:0] actions;
    logic [7:0] hits;

    // Compare two values only in the lanes whose mask bits are set.
    function automatic logic compare_masks(
        input logic [255:0] rule_with_unused_action,
        input logic [31:0] sip, input logic [31:0] dip,
        input logic [15:0] sport, input logic [15:0] dport, input logic [15:0] tci,
        input logic [1:0] depth
    );
        compare_masks =
           (((sip ^ rule_with_unused_action[255:224]) & rule_with_unused_action[223:192]) == 0)
        && (((dip ^ rule_with_unused_action[175:144]) & rule_with_unused_action[143:112]) == 0)
        && (((dport ^ rule_with_unused_action[111:96]) & rule_with_unused_action[95:80]) == 0)
        && (((tci ^ rule_with_unused_action[79:64]) & rule_with_unused_action[63:48]) == 0)
        && (((sport ^ rule_with_unused_action[47:32]) & rule_with_unused_action[31:16]) == 0)
        && (((depth ^ rule_with_unused_action[186:185]) & rule_with_unused_action[184:183]) == 0);
    endfunction

    assign write_ready = !rst;
    assign commit_ready = !rst;

    for (genvar index = 0; index < 8; index = index+1) begin : g_match
        logic [255:0] candidate;
        assign candidate = active_bank ? bank1[index] : bank0[index];
        assign actions[4*index +: 4] = candidate[190:187];
        assign hits[index] = (active_bank ? valid1[index] : valid0[index]) &&
                             compare_masks(candidate, source_ip, destination_ip,
                                           source_port, destination_port, outer_tci, vlan_depth);
    end

    always @* begin
        match_valid = 1'b0;
        match_drop = 1'b0;
        match_queue = '0;
        for (integer i = 0; i < 8; i = i+1)
            if (!match_valid && hits[i]) begin
                match_valid = 1'b1;
                match_drop = actions[4*i+3];
                match_queue = actions[4*i +: 3];
            end
    end

    // The old active table becomes the new staging table after a commit.
    always_ff @(posedge clk) begin
        if (rst) begin
            active_bank  <= 1'b0;
            active_epoch <= '0;
            valid0       <= '0;
            valid1       <= '0;
        end else begin
            if (write_valid && write_ready) begin
                if (active_bank) begin
                    bank0[write_index] <= write_rule;
                    valid0[write_index] <= write_rule[191];
                end else begin
                    bank1[write_index] <= write_rule;
                    valid1[write_index] <= write_rule[191];
                end
            end
            if (commit_valid && commit_ready) begin
                active_bank  <= !active_bank;
                active_epoch <= active_epoch + 1'b1;
            end
        end
    end
endmodule
