`timescale 1ns/1ps
`default_nettype none

// Commands edit the shadow table. COMMIT replaces the active policy on a
// single edge. The datapath samples the old policy on that edge.
module replay_impairment_rules #(
    parameter integer RULES = 4,
    parameter integer RUN_W = 32,
    parameter integer REV_W = 16,
    localparam integer KEY_W = $clog2(RULES+1),
    localparam integer RULE_W = 198+RUN_W
) (
    input wire clk, rst,
    input wire cfg_valid,
    output wire cfg_ready,
    input wire [1:0] cfg_op,
    input wire [REV_W-1:0] cfg_expected_revision,
    input wire [KEY_W-1:0] cfg_slot,
    input wire cfg_read_active,
    input wire [31:0] cfg_seed,
    input wire [RULE_W-1:0] cfg_rule,
    output wire rsp_valid,
    input wire rsp_ready,
    output logic [1:0] rsp_op,
    output logic [2:0] rsp_status,
    output logic [REV_W-1:0] rsp_revision,
    output logic [KEY_W-1:0] rsp_slot,
    output logic [31:0] rsp_seed,
    output logic [RULE_W-1:0] rsp_rule,

    input wire resolve_bank,
    input wire [RUN_W-1:0] resolve_run,
    input wire [15:0] resolve_tag, resolve_pass,
    input wire [31:0] resolve_ordinal,
    output logic [KEY_W-1:0] resolved_slot,
    output wire [REV_W-1:0] resolved_revision,
    output logic [1:0] resolved_action,
    output logic [32:0] resolved_delay,
    output logic [31:0] resolved_copy_gap,
    output logic [15:0] resolved_flip_offset,
    output logic [7:0] resolved_flip_mask
);
    localparam logic [1:0] WRITE = 0, READ = 1, CLEAR = 2, COMMIT = 3;
    localparam logic [2:0] OK = 0, STALE = 1, SLOT_RANGE = 2, BAD_RULE = 3, EXHAUSTED = 4;
    localparam logic [1:0] PASS = 0, CORRUPT = 3;
    localparam integer INDEX_W = RULES > 1 ? $clog2(RULES) : 1;

    typedef struct packed {
        logic enabled;
        logic [15:0] tag_value, tag_mask;
        logic any_run;
        logic [RUN_W-1:0] run_id;
        logic any_bank, bank;
        logic [31:0] first_ordinal, last_ordinal;
        logic [1:0] action;
        logic [31:0] delay_ticks;
        logic [7:0] jitter_mask;
        logic [31:0] copy_gap;
        logic [15:0] flip_offset;
        logic [7:0] flip_mask;
    } rule_t;

    rule_t bank0 [0:RULES-1];
    rule_t bank1 [0:RULES-1];
    wire rule_t incoming = cfg_rule;
    logic active_bank, response_pending;
    logic [REV_W-1:0] revision;
    logic [31:0] seed;
    logic [2:0] status;
    wire key_ok = int'(cfg_slot) < RULES;
    wire [INDEX_W-1:0] key = key_ok ? INDEX_W'(cfg_slot) : '0;
    wire read_bank = cfg_op == READ && cfg_read_active ? active_bank : !active_bank;
    wire [RULE_W-1:0] selected_rule = read_bank ? bank1[key] : bank0[key];
    wire config_fire = cfg_valid && cfg_ready;
    wire commit = config_fire && cfg_op == COMMIT && status == OK;
    wire clear = config_fire && cfg_op == CLEAR && status == OK;
    wire write_rule = config_fire && cfg_op == WRITE && status == OK;

    assign cfg_ready = !rst && (!response_pending || rsp_ready);
    assign rsp_valid = !rst && response_pending;
    assign resolved_revision = revision;

    function automatic logic [7:0] jitter_bits(input logic [31:0] value);
        logic [31:0] stage1, stage2;
        begin
            stage1 = value ^ (value << 13);
            stage2 = stage1 ^ (stage1 >> 17);
            jitter_bits = 8'(stage2 ^ (stage2 << 5));
        end
    endfunction

    wire [7:0] random_bits = jitter_bits(
        seed ^ 32'(resolve_run) ^ {resolve_tag,resolve_pass} ^ resolve_ordinal ^
        (resolve_bank ? 32'h9e3779b9 : 32'd0)
    );

    always @* begin
        status = OK;
        if (cfg_op != READ && cfg_expected_revision != revision)
            status = STALE;
        else if ((cfg_op == READ || cfg_op == WRITE) && !key_ok)
            status = SLOT_RANGE;
        else if (cfg_op == WRITE && incoming.enabled &&
                 (incoming.first_ordinal > incoming.last_ordinal ||
                  (incoming.action == CORRUPT && incoming.flip_mask == 0)))
            status = BAD_RULE;
        else if (cfg_op == COMMIT && (&revision))
            status = EXHAUSTED;
    end

    always @* begin
        rule_t candidate;
        resolved_slot = KEY_W'(RULES);
        resolved_action = PASS;
        resolved_delay = '0;
        resolved_copy_gap = '0;
        resolved_flip_offset = '0;
        resolved_flip_mask = '0;
        for (integer idx = 0; idx < RULES; idx++) begin
            candidate = active_bank ? bank1[idx] : bank0[idx];
            if (resolved_slot == KEY_W'(RULES) && candidate.enabled &&
                ((resolve_tag ^ candidate.tag_value) & candidate.tag_mask) == 0 &&
                (candidate.any_run || resolve_run == candidate.run_id) &&
                (candidate.any_bank || resolve_bank == candidate.bank) &&
                candidate.first_ordinal <= resolve_ordinal && resolve_ordinal <= candidate.last_ordinal) begin
                resolved_slot = KEY_W'(idx);
                resolved_action = candidate.action;
                resolved_delay = {1'b0,candidate.delay_ticks} +
                                 33'(random_bits & candidate.jitter_mask);
                resolved_copy_gap = candidate.copy_gap;
                resolved_flip_offset = candidate.flip_offset;
                resolved_flip_mask = candidate.flip_mask;
            end
        end
    end

    for (genvar idx = 0; idx < RULES; idx++) begin : entries
        wire update = write_rule && key == INDEX_W'(idx);
        always_ff @(posedge clk) begin
            if (rst || (clear && active_bank))
                bank0[idx] <= '0;
            else if (update && active_bank)
                bank0[idx] <= incoming;

            if (rst || (clear && !active_bank))
                bank1[idx] <= '0;
            else if (update && !active_bank)
                bank1[idx] <= incoming;
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            active_bank <= 0;
            response_pending <= 0;
            revision <= '0;
            seed <= '0;
            rsp_op <= '0;
            rsp_status <= '0;
            rsp_revision <= '0;
            rsp_slot <= '0;
            rsp_seed <= '0;
            rsp_rule <= '0;
        end else begin
            if (rsp_ready)
                response_pending <= 0;
            if (config_fire) begin
                response_pending <= 1;
                rsp_op <= cfg_op;
                rsp_status <= status;
                rsp_revision <= revision + REV_W'(commit);
                rsp_slot <= cfg_slot;
                rsp_seed <= commit ? cfg_seed : seed;
                rsp_rule <= key_ok && (cfg_op == READ || cfg_op == WRITE) ? selected_rule : '0;
            end
            if (commit) begin
                active_bank <= !active_bank;
                revision <= revision + 1'b1;
                seed <= cfg_seed;
            end
        end
    end
endmodule

`default_nettype wire
