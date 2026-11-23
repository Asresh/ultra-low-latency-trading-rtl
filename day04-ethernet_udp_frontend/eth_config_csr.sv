`timescale 1ns/1ps

module eth_config_csr (
    input  logic         clk,
    input  logic         rst,

    input  logic         cmd_valid,
    output logic         cmd_ready,
    input  logic         cmd_write,
    input  logic [7:0]   cmd_address,
    input  logic [31:0]  cmd_value,
    input  logic [3:0]   cmd_strobe,
    output logic         rsp_valid,
    input  logic         rsp_ready,
    output logic [1:0]   rsp_status,
    output logic [31:0]  rsp_value,

    output logic         rule_write_valid,
    input  logic         rule_write_ready,
    output logic [2:0]   rule_write_index,
    output logic [255:0] rule_write_data,
    output logic         rule_commit_valid,
    input  logic         rule_commit_ready,
    input  logic [7:0]   rule_epoch,

    output logic         stats_snapshot,
    output logic         stats_clear_live,
    output logic [4:0]   stats_index,
    input  logic [63:0]  stats_data,
    input  logic [31:0]  stats_snapshot_id
);
    localparam logic [1:0] SUCCESS = 2'd0, ADDRESS_ERROR = 2'd1,
                           ACCESS_ERROR = 2'd2, VALUE_ERROR = 2'd3;
    localparam logic [7:0] IDENTITY = 8'h00, VERSION = 8'h04,
                           EPOCH = 8'h08, RULE_INDEX = 8'h10,
                           RULE_PUSH = 8'h40, COMMIT = 8'h44,
                           SNAPSHOT = 8'h48, CLEAR_COUNTERS = 8'h4c,
                           STAT_SELECT = 8'h50, STAT_LOW = 8'h54,
                           STAT_HIGH = 8'h58, STAT_ID = 8'h5c;

    typedef enum logic [2:0] {
        ACCEPT, PUSH_RULE, SWAP_RULES, CAPTURE_STATS, CLEAR_STATS, RESPOND
    } cfg_state_t;

    cfg_state_t state;
    logic [255:0] staged_rule;
    logic [2:0] staged_index;
    logic [4:0] count_index;
    logic [31:0] write_mask, merged_reg;
    logic [31:0] current_reg;
    logic selected_word, accept, pulse_value;
    logic [2:0] word_index;

    for (genvar byte_index = 0; byte_index < 4; byte_index = byte_index+1) begin : g_strobe
        assign write_mask[byte_index*8 +: 8] = {8{cmd_strobe[byte_index]}};
    end

    assign cmd_ready = !rst && state == ACCEPT;
    assign rsp_valid = !rst && state == RESPOND;
    assign accept = cmd_valid && cmd_ready;
    assign selected_word = cmd_address[7:5] == 3'b001;
    assign word_index = cmd_address[4:2];
    assign pulse_value = cmd_strobe[0] && (cmd_value & write_mask) == 32'd1;
    assign merged_reg = (current_reg & ~write_mask) | (cmd_value & write_mask);

    assign rule_write_valid = !rst && state == PUSH_RULE;
    assign rule_write_index = staged_index;
    assign rule_write_data  = staged_rule;
    assign rule_commit_valid = !rst && state == SWAP_RULES;
    assign stats_snapshot = !rst && state == CAPTURE_STATS;
    assign stats_clear_live = !rst && state == CLEAR_STATS;
    assign stats_index = count_index;

    always @* begin
        case (cmd_address)
            IDENTITY: current_reg = 32'h45554634;  // EUF4
            VERSION: current_reg = 32'h00010000;
            EPOCH: current_reg = {24'd0,rule_epoch};
            RULE_INDEX: current_reg = {29'd0,staged_index};
            STAT_SELECT: current_reg = {27'd0,count_index};
            STAT_LOW: current_reg = stats_data[31:0];
            STAT_HIGH: current_reg = stats_data[63:32];
            STAT_ID: current_reg = stats_snapshot_id;
            default: current_reg = selected_word ? staged_rule[32*word_index +: 32] : 32'd0;
        endcase
    end

    // Register the bus status and data and move the CSR adapter to its response state.
    task automatic complete(input logic [1:0] code, input logic [31:0] value);
        begin
            rsp_status <= code;
            rsp_value  <= value;
            state      <= RESPOND;
        end
    endtask

    always_ff @(posedge clk) begin
        if (rst) begin
            state        <= ACCEPT;
            rsp_status   <= SUCCESS;
            rsp_value    <= '0;
            staged_rule  <= '0;
            staged_index <= '0;
            count_index  <= '0;
        end else begin
            case (state)
                ACCEPT: if (accept) begin
                    if (cmd_address[1:0] != 0)
                        complete(ADDRESS_ERROR, 32'd0);
                    else if (selected_word) begin
                        if (cmd_write)
                            staged_rule[32*word_index +: 32] <= merged_reg;
                        complete(SUCCESS, cmd_write ? merged_reg : current_reg);
                    end else if (!cmd_write) begin
                        case (cmd_address)
                            IDENTITY, VERSION, EPOCH, RULE_INDEX, STAT_SELECT,
                            STAT_LOW, STAT_HIGH, STAT_ID: complete(SUCCESS, current_reg);
                            RULE_PUSH, COMMIT, SNAPSHOT, CLEAR_COUNTERS:
                                complete(ACCESS_ERROR, 32'd0);
                            default: complete(ADDRESS_ERROR, 32'd0);
                        endcase
                    end else begin
                        case (cmd_address)
                            RULE_INDEX: begin
                                if (merged_reg <= 7) begin
                                    staged_index <= merged_reg[2:0];
                                    complete(SUCCESS, merged_reg);
                                end else
                                    complete(VALUE_ERROR, 32'd0);
                            end
                            STAT_SELECT: begin
                                if (merged_reg <= 31) begin
                                    count_index <= merged_reg[4:0];
                                    complete(SUCCESS, merged_reg);
                                end else
                                    complete(VALUE_ERROR, 32'd0);
                            end
                            RULE_PUSH, COMMIT, SNAPSHOT, CLEAR_COUNTERS: begin
                                if (!pulse_value)
                                    complete(VALUE_ERROR, 32'd0);
                                else begin
                                    case (cmd_address)
                                        RULE_PUSH: state <= PUSH_RULE;
                                        COMMIT: state <= SWAP_RULES;
                                        SNAPSHOT: state <= CAPTURE_STATS;
                                        default: state <= CLEAR_STATS;
                                    endcase
                                end
                            end
                            IDENTITY, VERSION, EPOCH, STAT_LOW, STAT_HIGH, STAT_ID:
                                complete(ACCESS_ERROR, 32'd0);
                            default: complete(ADDRESS_ERROR, 32'd0);
                        endcase
                    end
                end
                PUSH_RULE: if (rule_write_ready)
                    complete(SUCCESS, 32'd0);
                SWAP_RULES: if (rule_commit_ready)
                    complete(SUCCESS, 32'd0);
                CAPTURE_STATS: complete(SUCCESS, 32'd0);
                CLEAR_STATS: complete(SUCCESS, 32'd0);
                RESPOND: if (rsp_ready)
                    state <= ACCEPT;
                default: state <= ACCEPT;
            endcase
        end
    end
endmodule
