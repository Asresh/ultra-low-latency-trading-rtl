`timescale 1ns/1ps
`default_nettype none

// Apply deltas from accepted native gateway decisions. An epoch rolls only
// executed turnover. Live reservations and configured ceilings survive it.
module risk_portfolio_ledger #(
    parameter integer ACCOUNTS = 2,
    parameter integer SLOTS = 8,
    parameter integer AMOUNT_W = 40,
    parameter integer TOTAL_W = 48,
    localparam integer ACCOUNT_W = ACCOUNTS > 1 ? $clog2(ACCOUNTS) : 1,
    localparam integer COUNT_W = $clog2(SLOTS + 1)
) (
    input  wire                         clk,
    input  wire                         rst,

    // One request identity remains selected through check and accounting.
    input  wire [ACCOUNT_W-1:0]         account,
    input  wire                         should_check,
    input  wire [AMOUNT_W-1:0]          prior_reserve,
    input  wire [AMOUNT_W-1:0]          proposed_reserve,
    input  wire                         proposed_new,
    output logic [2:0]                  denial,

    input  wire                         apply_delta,
    input  wire [AMOUNT_W-1:0]          committed_reserve,
    input  wire                         prior_live,
    input  wire                         committed_live,
    input  wire [AMOUNT_W-1:0]          committed_execution,
    output wire [TOTAL_W-1:0]           account_gross,
    output wire [TOTAL_W-1:0]           account_turnover,
    output wire [COUNT_W-1:0]           account_orders,
    output wire [TOTAL_W-1:0]           firm_gross,
    output wire [TOTAL_W-1:0]           firm_turnover,
    output wire [COUNT_W-1:0]           firm_orders,

    // A control operation is serialized with request admission by the top.
    input  wire                         ctl_fire,
    input  wire [1:0]                   ctl_op,
    input  wire                         ctl_firm,
    input  wire [ACCOUNT_W-1:0]         ctl_account,
    input  wire [2:0]                   ctl_mask,
    input  wire [TOTAL_W-1:0]           ctl_gross_limit,
    input  wire [COUNT_W-1:0]           ctl_count_limit,
    input  wire [TOTAL_W-1:0]           ctl_usage_limit,
    input  wire [31:0]                  ctl_epoch,
    output logic [2:0]                  ctl_status,
    output wire [2:0]                   view_mask,
    output wire [TOTAL_W-1:0]           view_gross_limit,
    output wire [COUNT_W-1:0]           view_count_limit,
    output wire [TOTAL_W-1:0]           view_usage_limit,
    output wire [TOTAL_W-1:0]           view_gross,
    output wire [TOTAL_W-1:0]           view_turnover,
    output wire [COUNT_W-1:0]           view_orders,
    output wire [31:0]                  view_epoch,
    output logic [31:0]                 epoch
);
    localparam integer SCOPE_W = $clog2(ACCOUNTS + 1);
    localparam [SCOPE_W-1:0] FIRM = SCOPE_W'(ACCOUNTS);
    localparam [1:0] CTL_SET = 0, CTL_READ = 1, CTL_ROLL = 2;
    localparam [2:0] CTL_OK = 0, CTL_BAD_SCOPE = 1, CTL_BAD_COUNT = 2,
                     CTL_STALE_EPOCH = 3, CTL_BAD_OP = 4;
    localparam [2:0] ACCEPT = 0, ACCOUNT_GROSS = 1, ACCOUNT_COUNT = 2,
                     ACCOUNT_USAGE = 3, FIRM_GROSS = 4, FIRM_COUNT = 5,
                     FIRM_USAGE = 6;

    logic [TOTAL_W-1:0] gross [0:ACCOUNTS];
    logic [TOTAL_W-1:0] turnover [0:ACCOUNTS];
    logic [COUNT_W-1:0] orders [0:ACCOUNTS];
    logic [2:0] mask [0:ACCOUNTS];
    logic [TOTAL_W-1:0] gross_limit [0:ACCOUNTS];
    logic [COUNT_W-1:0] count_limit [0:ACCOUNTS];
    logic [TOTAL_W-1:0] usage_limit [0:ACCOUNTS];

    wire account_ok = {1'b0, account} < (ACCOUNT_W+1)'(ACCOUNTS);
    wire [SCOPE_W-1:0] a_idx = account_ok ? SCOPE_W'(account) : '0;
    wire ctl_scope_ok = ctl_firm ? ctl_account == '0 :
                                       {1'b0, ctl_account} < (ACCOUNT_W+1)'(ACCOUNTS);
    wire [SCOPE_W-1:0] ctl_idx = ctl_firm ? FIRM : ctl_scope_ok ? SCOPE_W'(ctl_account) : '0;
    wire allow_control = ctl_fire && ctl_status == CTL_OK;
    wire set_policy = allow_control && ctl_op == CTL_SET;
    wire roll_epoch = allow_control && ctl_op == CTL_ROLL;
    wire update = apply_delta && account_ok;

    // A projected gross stays positive: the caller passes an old reserve
    // from the selected live slot or zero for a fresh order.
    wire [TOTAL_W:0] candidate_account_gross = {1'b0, gross[a_idx]}
                         - (TOTAL_W+1)'(prior_reserve) + (TOTAL_W+1)'(proposed_reserve);
    wire [TOTAL_W:0] candidate_firm_gross = {1'b0, gross[FIRM]}
                         - (TOTAL_W+1)'(prior_reserve) + (TOTAL_W+1)'(proposed_reserve);
    wire [TOTAL_W+1:0] candidate_account_usage =
        {1'b0, candidate_account_gross} + (TOTAL_W+2)'(turnover[a_idx]);
    wire [TOTAL_W+1:0] candidate_firm_usage =
        {1'b0, candidate_firm_gross} + (TOTAL_W+2)'(turnover[FIRM]);
    wire [COUNT_W:0] candidate_account_count = {1'b0, orders[a_idx]} + (COUNT_W+1)'(proposed_new);
    wire [COUNT_W:0] candidate_firm_count = {1'b0, orders[FIRM]} + (COUNT_W+1)'(proposed_new);

    always @* begin
        denial = ACCEPT;
        if (should_check && account_ok) begin
            if (mask[a_idx][0] && candidate_account_gross > {1'b0, gross_limit[a_idx]})
                denial = ACCOUNT_GROSS;
            else if (mask[a_idx][1] && candidate_account_count > {1'b0, count_limit[a_idx]})
                denial = ACCOUNT_COUNT;
            else if (mask[a_idx][2] && candidate_account_usage > {2'b0, usage_limit[a_idx]})
                denial = ACCOUNT_USAGE;
            else if (mask[FIRM][0] && candidate_firm_gross > {1'b0, gross_limit[FIRM]})
                denial = FIRM_GROSS;
            else if (mask[FIRM][1] && candidate_firm_count > {1'b0, count_limit[FIRM]})
                denial = FIRM_COUNT;
            else if (mask[FIRM][2] && candidate_firm_usage > {2'b0, usage_limit[FIRM]})
                denial = FIRM_USAGE;
        end
    end

    always @* begin
        ctl_status = CTL_OK;
        case (ctl_op)
            CTL_SET: begin
                if (!ctl_scope_ok)
                    ctl_status = CTL_BAD_SCOPE;
                else if (ctl_mask[1] && ctl_count_limit > COUNT_W'(SLOTS))
                    ctl_status = CTL_BAD_COUNT;
            end
            CTL_READ: if (!ctl_scope_ok)
                ctl_status = CTL_BAD_SCOPE;
            CTL_ROLL: begin
                if (!ctl_firm || !ctl_scope_ok)
                    ctl_status = CTL_BAD_SCOPE;
                else if (ctl_epoch <= epoch)
                    ctl_status = CTL_STALE_EPOCH;
            end
            default: ctl_status = CTL_BAD_OP;
        endcase
    end

    wire [TOTAL_W-1:0] remove_amount = TOTAL_W'(prior_reserve);
    wire [TOTAL_W-1:0] add_amount = TOTAL_W'(committed_reserve);
    wire [TOTAL_W-1:0] execution_amount = TOTAL_W'(committed_execution);
    wire [COUNT_W-1:0] former_count = COUNT_W'(prior_live);
    wire [COUNT_W-1:0] latest_count = COUNT_W'(committed_live);

    wire [TOTAL_W-1:0] next_account_gross = gross[a_idx] - remove_amount + add_amount;
    wire [TOTAL_W-1:0] next_firm_gross = gross[FIRM] - remove_amount + add_amount;
    wire [TOTAL_W-1:0] next_account_turnover = turnover[a_idx] + execution_amount;
    wire [TOTAL_W-1:0] next_firm_turnover = turnover[FIRM] + execution_amount;
    wire [COUNT_W-1:0] next_account_orders = orders[a_idx] - former_count + latest_count;
    wire [COUNT_W-1:0] next_firm_orders = orders[FIRM] - former_count + latest_count;

    assign account_gross = !account_ok ? '0 : update ? next_account_gross : gross[a_idx];
    assign account_turnover = !account_ok ? '0 : update ? next_account_turnover : turnover[a_idx];
    assign account_orders = !account_ok ? '0 : update ? next_account_orders : orders[a_idx];
    assign firm_gross = update ? next_firm_gross : gross[FIRM];
    assign firm_turnover = update ? next_firm_turnover : turnover[FIRM];
    assign firm_orders = update ? next_firm_orders : orders[FIRM];

    assign view_mask = !ctl_scope_ok ? '0 : set_policy ? ctl_mask : mask[ctl_idx];
    assign view_gross_limit = !ctl_scope_ok ? '0 : set_policy ?
                              (ctl_mask[0] ? ctl_gross_limit : '0) : gross_limit[ctl_idx];
    assign view_count_limit = !ctl_scope_ok ? '0 : set_policy ?
                              (ctl_mask[1] ? ctl_count_limit : '0) : count_limit[ctl_idx];
    assign view_usage_limit = !ctl_scope_ok ? '0 : set_policy ?
                              (ctl_mask[2] ? ctl_usage_limit : '0) : usage_limit[ctl_idx];
    assign view_gross = ctl_scope_ok ? gross[ctl_idx] : '0;
    assign view_turnover = !ctl_scope_ok || roll_epoch ? '0 : turnover[ctl_idx];
    assign view_orders = ctl_scope_ok ? orders[ctl_idx] : '0;
    assign view_epoch = roll_epoch ? ctl_epoch : epoch;

    always @(posedge clk) begin
        if (rst) begin
            epoch <= 0;
            for (integer i = 0; i <= ACCOUNTS; i = i + 1) begin
                gross[i] <= 0;
                turnover[i] <= 0;
                orders[i] <= 0;
                mask[i] <= 0;
                gross_limit[i] <= 0;
                count_limit[i] <= 0;
                usage_limit[i] <= 0;
            end
        end else begin
            if (update) begin
                gross[a_idx] <= next_account_gross;
                turnover[a_idx] <= next_account_turnover;
                orders[a_idx] <= next_account_orders;
                gross[FIRM] <= next_firm_gross;
                turnover[FIRM] <= next_firm_turnover;
                orders[FIRM] <= next_firm_orders;
            end
            if (set_policy) begin
                mask[ctl_idx] <= ctl_mask;
                gross_limit[ctl_idx] <= view_gross_limit;
                count_limit[ctl_idx] <= view_count_limit;
                usage_limit[ctl_idx] <= view_usage_limit;
            end
            if (roll_epoch) begin
                epoch <= ctl_epoch;
                for (integer i = 0; i <= ACCOUNTS; i = i + 1)
                    turnover[i] <= 0;
            end
        end
    end
endmodule

`default_nettype wire
