`timescale 1ns/1ps
`default_nettype none

// Separate bounded tenant queues defer rate-limited messages without
// transferring the head to the output until all three budgets admit it.
module deferred_rate_scheduler #(
    parameter integer GROUPS = 3,
    parameter integer SESSIONS = 7,
    parameter integer DEPTH = 4,
    parameter integer TOKEN_W = 12,
    parameter integer TTL_W = 12,
    parameter integer TAG_W = 16,
    parameter integer DATA_W = 32,
    localparam integer GROUP_W = $clog2(GROUPS+1),
    localparam integer SESSION_W = $clog2(SESSIONS+1),
    localparam integer COUNT_W = $clog2(DEPTH+1)
) (
    input  wire clk,
    input  wire rst,
    input  wire a_valid,
    output wire a_ready,
    input  wire [2:0] a_op,
    input  wire [SESSION_W-1:0] a_session,
    input  wire [GROUP_W-1:0] a_group,
    input  wire [TOKEN_W-1:0] a_capacity,
    input  wire [TOKEN_W-1:0] a_rate,
    input  wire [TOKEN_W-1:0] a_reserve,
    input  wire [TOKEN_W-1:0] a_quantum,
    input  wire a_enable,
    input  wire a_critical,
    input  wire [TAG_W-1:0] a_tag,
    input  wire s_valid,
    output wire s_ready,
    input  wire [SESSION_W-1:0] s_session,
    input  wire [TOKEN_W-1:0] s_cost,
    input  wire [TTL_W-1:0] s_ttl,
    input  wire [TAG_W-1:0] s_tag,
    input  wire [DATA_W-1:0] s_data,
    output wire s_rejected,
    output wire m_valid,
    input  wire m_ready,
    output logic m_admin,
    output logic [2:0] m_op,
    output logic [3:0] m_status,
    output logic [2:0] m_detail,
    output logic [SESSION_W-1:0] m_session,
    output logic [GROUP_W-1:0] m_group,
    output logic [TAG_W-1:0] m_tag,
    output logic [DATA_W-1:0] m_data,
    output logic [TOKEN_W-1:0] m_cost,
    output logic [TTL_W-1:0] m_ttl,
    output logic [TOKEN_W-1:0] m_deficit,
    output logic [COUNT_W-1:0] m_depth,
    output logic [3*TOKEN_W-1:0] m_balances,
    output wire [SESSIONS*COUNT_W-1:0] occupancy
);
    localparam integer SESSION_IDX_W = SESSIONS > 1 ? $clog2(SESSIONS) : 1;
    localparam integer GROUP_IDX_W = GROUPS > 1 ? $clog2(GROUPS) : 1;
    localparam integer PTR_W = DEPTH > 1 ? $clog2(DEPTH) : 1;
    localparam logic [2:0] SET_PORT = 0, SET_GROUP = 1, SET_SESSION = 2,
        GET_PORT = 3, GET_GROUP = 4, GET_SESSION = 5;
    localparam logic [3:0] OK = 0, PASS = 1, EXPIRED = 2, DISABLED = 3,
        IMPOSSIBLE = 4, BAD_KEY = 5, BAD_FIELDS = 6, BUSY = 7;

    logic [PTR_W-1:0] head [0:SESSIONS-1], tail [0:SESSIONS-1];
    logic [COUNT_W-1:0] count [0:SESSIONS-1];
    logic [DEPTH-1:0] live [0:SESSIONS-1];
    logic [TTL_W-1:0] ttl [0:SESSIONS-1][0:DEPTH-1];
    logic [TOKEN_W-1:0] cost [0:SESSIONS-1][0:DEPTH-1];
    logic [TAG_W-1:0] tags [0:SESSIONS-1][0:DEPTH-1];
    logic [DATA_W-1:0] payload [0:SESSIONS-1][0:DEPTH-1];
    logic [TOKEN_W-1:0] deficit [0:SESSIONS-1], quantum [0:SESSIONS-1];
    logic [GROUP_W-1:0] parent [0:SESSIONS-1];
    logic critical [0:SESSIONS-1];

    logic [SESSION_IDX_W-1:0] cursor;
    logic visit_credited, result_held;
    wire out_space = !result_held || m_ready;
    assign m_valid = !rst && result_held;
    assign a_ready = !rst && out_space;
    wire manage = a_valid && a_ready;
    wire [SESSION_IDX_W-1:0] admin_session = SESSION_IDX_W'(a_session);
    wire [GROUP_IDX_W-1:0] admin_group = GROUP_IDX_W'(a_group);
    wire admin_session_valid = a_session < SESSION_W'(SESSIONS);
    wire admin_group_valid = a_group < GROUP_W'(GROUPS);
    wire session_op = a_op == SET_SESSION || a_op == GET_SESSION;
    wire group_op = a_op == SET_GROUP || a_op == GET_GROUP;
    wire writes = a_op <= SET_SESSION;
    logic [3:0] admin_status;
    always @* begin
        admin_status = OK;
        if ((session_op && !admin_session_valid) ||
            ((group_op || (a_op == SET_SESSION && a_enable)) && !admin_group_valid))
            admin_status = BAD_KEY;
        else if (a_op > GET_SESSION || (!session_op && a_session != 0) ||
                 (!group_op && a_op != SET_SESSION && a_group != 0))
            admin_status = BAD_FIELDS;
        else if (writes && a_enable) begin
            if (a_capacity == 0 || a_rate > a_capacity || a_reserve > a_capacity ||
                (session_op ? a_quantum == 0 || a_reserve != 0 : a_quantum != 0 || a_critical))
                admin_status = BAD_FIELDS;
        end else if ({a_capacity,a_rate,a_reserve,a_quantum,a_enable,a_critical} != 0 ||
                     (a_op == SET_SESSION && a_group != 0)) begin
            admin_status = BAD_FIELDS;
        end
        if (admin_status == OK && a_op == SET_SESSION && count[admin_session] != 0)
            admin_status = BUSY;
    end

    wire set_policy = manage && admin_status == OK && writes;
    wire [GROUP_IDX_W-1:0] chosen_group = GROUP_IDX_W'(parent[cursor]);
    wire [GROUP_W-1:0] actual_group = parent[cursor];
    wire [PTR_W-1:0] read_index = head[cursor];
    wire [TOKEN_W-1:0] head_cost = cost[cursor][read_index];
    wire [TTL_W-1:0] head_ttl = ttl[cursor][read_index];
    wire queued = count[cursor] != 0;

    wire port_live;
    wire [GROUPS-1:0] group_live;
    wire [SESSIONS-1:0] session_live;
    wire [TOKEN_W-1:0] port_cap, port_floor, port_tokens;
    wire [TOKEN_W-1:0] group_cap [0:GROUPS-1], group_floor [0:GROUPS-1], group_tokens [0:GROUPS-1];
    wire [TOKEN_W-1:0] session_cap [0:SESSIONS-1], session_tokens [0:SESSIONS-1];

    wire [TOKEN_W:0] sum_deficit = {1'b0,deficit[cursor]} + {1'b0,quantum[cursor]};
    wire [TOKEN_W-1:0] credited_deficit = visit_credited ? deficit[cursor] :
        (sum_deficit[TOKEN_W] ? {TOKEN_W{1'b1}} : sum_deficit[TOKEN_W-1:0]);
    wire [TOKEN_W:0] needed_port = {1'b0,head_cost} +
        (critical[cursor] ? (TOKEN_W+1)'(0) : {1'b0,port_floor});
    wire [TOKEN_W:0] needed_group = {1'b0,head_cost} +
        (critical[cursor] ? (TOKEN_W+1)'(0) : {1'b0,group_floor[chosen_group]});
    wire [2:0] disabled = ~{session_live[cursor],group_live[chosen_group],port_live};
    wire [2:0] impossible = {
        head_cost > session_cap[cursor],
        needed_group > {1'b0,group_cap[chosen_group]},
        needed_port > {1'b0,port_cap}
    };
    wire affordable = needed_port <= {1'b0,port_tokens} &&
        needed_group <= {1'b0,group_tokens[chosen_group]} && head_cost <= session_tokens[cursor];
    wire enough_deficit = credited_deficit >= head_cost;
    wire can_visit = !rst && out_space && !manage;
    logic [3:0] scheduled_status;
    logic [2:0] scheduled_detail;
    logic finish;
    always @* begin
        scheduled_status = PASS;
        scheduled_detail = '0;
        finish = 1;
        if (!queued)
            finish = 0;
        else if (head_ttl == 0)
            scheduled_status = EXPIRED;
        else if (disabled != 0) begin
            scheduled_status = DISABLED;
            scheduled_detail = disabled;
        end else if (impossible != 0) begin
            scheduled_status = IMPOSSIBLE;
            scheduled_detail = impossible;
        end else if (!affordable || !enough_deficit) begin
            finish = 0;
        end
    end
    wire pop = can_visit && finish;
    wire admitted = pop && scheduled_status == PASS;
    wire [TOKEN_W-1:0] debit = admitted ? head_cost : '0;
    wire [TOKEN_W-1:0] left_deficit = !queued || (pop && count[cursor] == 1) ? '0 :
        (admitted ? credited_deficit-head_cost : (pop ? deficit[cursor] : credited_deficit));

    wire good_source = s_session < SESSION_W'(SESSIONS) && s_cost != 0 && s_ttl != 0;
    wire [SESSION_IDX_W-1:0] input_index = SESSION_IDX_W'(s_session);
    assign s_ready = !rst && (!good_source ||
        count[input_index] < COUNT_W'(DEPTH) || (pop && input_index == cursor));
    assign s_rejected = s_valid && s_ready && !good_source;
    wire enqueue = s_valid && s_ready && good_source;

    function automatic logic [PTR_W-1:0] advance(input logic [PTR_W-1:0] ptr);
        advance = ptr == PTR_W'(DEPTH-1) ? '0 : ptr+1'b1;
    endfunction

    eager_rate_bucket #(.TOKEN_W(TOKEN_W)) port_bucket (
        .clk,.rst,.configure(set_policy && a_op == SET_PORT),.new_enable(a_enable),
        .new_capacity(a_capacity),.new_rate(a_rate),.new_reserve(a_reserve),.debit,
        .enabled(port_live),.capacity(port_cap),.reserve(port_floor),.tokens(port_tokens)
    );
    for (genvar group = 0; group < GROUPS; group++) begin : group_budget
        eager_rate_bucket #(.TOKEN_W(TOKEN_W)) bucket (
            .clk,.rst,.configure(set_policy && a_op == SET_GROUP && admin_group == GROUP_IDX_W'(group)),
            .new_enable(a_enable),.new_capacity(a_capacity),.new_rate(a_rate),.new_reserve(a_reserve),
            .debit(chosen_group == GROUP_IDX_W'(group) ? debit : TOKEN_W'(0)),
            .enabled(group_live[group]),.capacity(group_cap[group]),.reserve(group_floor[group]),
            .tokens(group_tokens[group])
        );
    end
    for (genvar key = 0; key < SESSIONS; key++) begin : tenant
        localparam logic [SESSION_IDX_W-1:0] KEY = SESSION_IDX_W'(key);
        wire owned = cursor == KEY;
        wire add = enqueue && input_index == KEY;
        wire remove = pop && owned;
        wire set_session = set_policy && a_op == SET_SESSION && admin_session == KEY;
        wire [TOKEN_W-1:0] unused_session_reserve;
        assign occupancy[key*COUNT_W+:COUNT_W] = count[key];
        eager_rate_bucket #(.TOKEN_W(TOKEN_W)) bucket (
            .clk,.rst,.configure(set_session),.new_enable(a_enable),.new_capacity(a_capacity),
            .new_rate(a_rate),.new_reserve(TOKEN_W'(0)),.debit(owned ? debit : TOKEN_W'(0)),
            .enabled(session_live[key]),.capacity(session_cap[key]),.reserve(unused_session_reserve),
            .tokens(session_tokens[key])
        );
        always_ff @(posedge clk) begin
            if (rst) begin
                {head[key],tail[key],count[key],live[key]} <= '0;
                {quantum[key],deficit[key],parent[key],critical[key]} <= '0;
            end else begin
                if (set_session) begin
                    quantum[key] <= a_quantum;
                    parent[key] <= a_group;
                    critical[key] <= a_critical;
                    deficit[key] <= '0;
                end else if (can_visit && owned) begin
                    deficit[key] <= left_deficit;
                end
                if (add != remove)
                    count[key] <= add ? count[key]+1'b1 : count[key]-1'b1;
                if (remove) begin
                    head[key] <= advance(head[key]);
                    live[key][head[key]] <= 0;
                end
                for (integer element = 0; element < DEPTH; element++)
                    if (live[key][element] && ttl[key][element] != 0)
                        ttl[key][element] <= ttl[key][element]-1'b1;
                if (add) begin
                    tail[key] <= advance(tail[key]);
                    live[key][tail[key]] <= 1;
                    ttl[key][tail[key]] <= s_ttl;
                    cost[key][tail[key]] <= s_cost;
                    tags[key][tail[key]] <= s_tag;
                    payload[key][tail[key]] <= s_data;
                end
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            cursor <= '0;
            visit_credited <= 0;
        end else if (can_visit) begin
            if (admitted && count[cursor] > 1) begin
                visit_credited <= 1;
            end else begin
                visit_credited <= 0;
                cursor <= cursor == SESSION_IDX_W'(SESSIONS-1) ? '0 : cursor+1'b1;
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            result_held <= 0;
            {m_admin,m_op,m_status,m_detail,m_session,m_group,m_tag,m_data,m_cost,m_ttl,
             m_deficit,m_depth,m_balances} <= '0;
        end else if (out_space) begin
            result_held <= manage || pop;
            if (manage) begin
                {m_admin,m_op,m_status,m_detail,m_session,m_group,m_tag} <=
                    {1'b1,a_op,admin_status,3'd0,a_session,a_group,a_tag};
                {m_data,m_cost,m_ttl,m_deficit,m_depth,m_balances} <= '0;
                if (admin_status == OK) begin
                    case (a_op)
                        SET_PORT, GET_PORT: m_balances[0+:TOKEN_W] <= writes ? a_capacity : port_tokens;
                        SET_GROUP, GET_GROUP: m_balances[TOKEN_W+:TOKEN_W] <=
                            writes ? a_capacity : group_tokens[admin_group];
                        SET_SESSION, GET_SESSION: begin
                            m_balances[2*TOKEN_W+:TOKEN_W] <=
                                writes ? a_capacity : session_tokens[admin_session];
                            m_group <= writes ? a_group : parent[admin_session];
                            m_depth <= count[admin_session];
                            m_deficit <= writes ? TOKEN_W'(0) : deficit[admin_session];
                        end
                        default: ;
                    endcase
                end
            end else if (pop) begin
                {m_admin,m_op,m_status,m_detail,m_session,m_group,m_tag} <=
                    {1'b0,3'd6,scheduled_status,scheduled_detail,SESSION_W'(cursor),
                     actual_group,tags[cursor][read_index]};
                m_data <= payload[cursor][read_index];
                m_cost <= head_cost;
                m_ttl <= head_ttl;
                m_deficit <= left_deficit;
                m_depth <= count[cursor];
                m_balances <= {session_tokens[cursor]-debit,group_tokens[chosen_group]-debit,port_tokens-debit};
            end
        end
    end

`ifdef FORMAL_DEFERRED
`include "deferred_properties.svh"
`endif
endmodule

`default_nettype wire
