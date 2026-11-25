`timescale 1ns/1ps
`default_nettype none

// Admission updates the port, group and session on the same accepted command.
module hierarchical_rate_limiter #(
    parameter integer GROUPS = 3,
    parameter integer SESSIONS = 7,
    parameter integer TICK_W = 12,
    parameter integer TOKEN_W = 12,
    parameter integer COUNT_W = 16,
    parameter integer TAG_W = 16,
    localparam integer GROUP_W = $clog2(GROUPS + 1),
    localparam integer SESSION_W = $clog2(SESSIONS + 1),
    localparam integer SNAPSHOT_W = 1 + TICK_W + 5*TOKEN_W + 2*COUNT_W + 2
) (
    input wire clk,
    input wire rst,
    input wire s_valid,
    output wire s_ready,
    input wire [2:0] s_op,
    input wire [SESSION_W-1:0] s_session,
    input wire [GROUP_W-1:0] s_group,
    input wire [TICK_W-1:0] s_tick,
    input wire [TOKEN_W-1:0] s_amount,
    input wire [TOKEN_W-1:0] s_rate,
    input wire [TOKEN_W-1:0] s_reserve,
    input wire s_enable,
    input wire s_critical,
    input wire [TAG_W-1:0] s_tag,
    output wire m_valid,
    input wire m_ready,
    output logic [2:0] m_op,
    output logic [SESSION_W-1:0] m_session,
    output logic [GROUP_W-1:0] m_group,
    output logic [TAG_W-1:0] m_tag,
    output logic [3:0] m_status,
    output logic [2:0] m_scope,
    output logic [2:0] m_detail,
    output logic m_critical,
    output logic [SNAPSHOT_W-1:0] m_port,
    output logic [SNAPSHOT_W-1:0] m_group_state,
    output logic [SNAPSHOT_W-1:0] m_session_state
);
    localparam logic [2:0] CONFIG_PORT = 3'd0, CONFIG_GROUP = 3'd1, CONFIG_SESSION = 3'd2,
                           INSPECT_PORT = 3'd3, INSPECT_GROUP = 3'd4, INSPECT_SESSION = 3'd5,
                           OFFER = 3'd6;
    localparam logic [3:0] OK = 4'd0, PASS = 4'd1, LIMITED = 4'd2, BAD_SESSION = 4'd3,
                           BAD_FIELDS = 4'd4, DISABLED = 4'd5, BAD_TICK = 4'd6, BAD_GROUP = 4'd7;
    localparam integer GROUP_ADDR_W = GROUPS > 1 ? $clog2(GROUPS) : 1;
    localparam integer SESSION_ADDR_W = SESSIONS > 1 ? $clog2(SESSIONS) : 1;

    typedef struct packed {
        logic enabled;
        logic [TICK_W-1:0] anchor;
        logic [TOKEN_W-1:0] capacity;
        logic [TOKEN_W-1:0] rate;
        logic [TOKEN_W-1:0] reserve_floor;
        logic [TOKEN_W-1:0] balance;
        logic [COUNT_W-1:0] passed;
        logic [COUNT_W-1:0] blocked;
        logic [1:0] saturated;
    } bucket_t;

    bucket_t port_bucket;
    bucket_t groups [0:GROUPS-1];
    bucket_t sessions [0:SESSIONS-1];
    logic [GROUP_W-1:0] parent [0:SESSIONS-1];
    logic critical_session [0:SESSIONS-1];
    logic [2:0] scope, detail, update_mask;
    logic [3:0] status;
    logic is_config, is_session, is_group, is_offer;
    logic effective_critical, output_critical;
    logic fields_ok;
    logic [GROUP_W-1:0] selected_group;
    logic [SESSION_ADDR_W-1:0] session_index;
    logic [GROUP_ADDR_W-1:0] group_index;
    logic session_valid, group_valid;
    logic [2:0] enabled, stale, lacking;
    logic [TOKEN_W-1:0] fresh [0:2];
    logic [TOKEN_W-1:0] before_charge [0:2];
    bucket_t selected [0:2];
    bucket_t updated [0:2];
    logic result_valid;

    assign m_valid = !rst && result_valid;
    assign s_ready = !rst && (!result_valid || m_ready);

    always @* begin
        is_config = s_op <= CONFIG_SESSION;
        is_session = s_op == CONFIG_SESSION || s_op == INSPECT_SESSION;
        is_group = s_op == CONFIG_GROUP || s_op == INSPECT_GROUP;
        is_offer = s_op == OFFER;
        scope = '0;
        if (is_offer)
            scope = 3'b111;
        else if (is_session)
            scope = 3'b100;
        else if (is_group)
            scope = 3'b010;
        else if (s_op == CONFIG_PORT || s_op == INSPECT_PORT)
            scope = 3'b001;

        session_valid = s_session < SESSION_W'(SESSIONS);
        session_index = SESSION_ADDR_W'(s_session);
        effective_critical = 1'b0;
        if (session_valid && (is_offer || is_session))
            effective_critical = critical_session[session_index];
        selected_group = s_group;
        if (session_valid && (is_offer || s_op == INSPECT_SESSION))
            selected_group = parent[session_index];
        group_valid = selected_group < GROUP_W'(GROUPS);
        group_index = GROUP_ADDR_W'(selected_group);

        selected[0] = scope[0] ? port_bucket : '0;
        selected[1] = scope[1] && group_valid ? groups[group_index] : '0;
        selected[2] = scope[2] && session_valid ? sessions[session_index] : '0;
    end

    for (genvar layer = 0; layer < 3; layer++) begin : refill_layers
        wire bucket_t row = selected[layer];
        wire [2*COUNT_W+1:0] unused_accounting = {row.passed, row.blocked, row.saturated};
        wire chronological;
        wire [TOKEN_W:0] needed = {1'b0, s_amount} +
            (effective_critical ? (TOKEN_W+1)'(0) : {1'b0, row.reserve_floor});
        assign enabled[layer] = row.enabled;
        assign stale[layer] = row.enabled && !chronological;
        assign lacking[layer] = {1'b0, fresh[layer]} < needed;
        rate_refill #(.TICK_W(TICK_W), .TOKEN_W(TOKEN_W)) arithmetic (
            .tick(s_tick), .anchor(row.anchor), .balance(row.balance),
            .capacity(row.capacity), .rate(row.rate),
            .chronological(chronological), .available(fresh[layer])
        );
    end

    always @* begin
        fields_ok = s_op != 3'd7;
        if (!is_session && !is_offer && s_session != 0)
            fields_ok = 1'b0;
        if (!is_group && s_op != CONFIG_SESSION && s_group != 0)
            fields_ok = 1'b0;
        if (is_config) begin
            if (s_enable)
                fields_ok = fields_ok && s_amount != 0 && s_rate <= s_amount &&
                    s_reserve <= s_amount && (is_session ? s_reserve == 0 : !s_critical);
            else
                fields_ok = fields_ok && {s_amount, s_rate, s_reserve, s_critical} == 0 &&
                    (!is_session || s_group == 0);
        end else begin
            fields_ok = fields_ok && {s_rate, s_reserve, s_critical, s_enable} == 0 &&
                ((s_amount != 0) == is_offer);
        end

        status = OK;
        detail = '0;
        update_mask = '0;
        output_critical = effective_critical;
        if ((is_session || is_offer) && !session_valid)
            status = BAD_SESSION;
        else if ((is_group || (s_op == CONFIG_SESSION && s_enable)) && !group_valid)
            status = BAD_GROUP;
        else if (!fields_ok)
            status = BAD_FIELDS;
        else if (is_config) begin
            update_mask = scope;
            if (is_session)
                output_critical = s_critical;
        end else if (is_offer && enabled != 3'b111) begin
            status = DISABLED;
            detail = ~enabled;
        end else if ((stale & scope) != 0) begin
            status = BAD_TICK;
            detail = stale & scope;
        end else begin
            update_mask = scope & enabled;
            if (is_offer) begin
                detail = lacking;
                status = lacking == 0 ? PASS : LIMITED;
            end
        end
    end

    always @* begin : derive_state
        bucket_t current;
        bucket_t next_row;
        for (integer layer = 0; layer < 3; layer++) begin
            current = selected[layer];
            next_row = current;
            before_charge[layer] = current.balance;
            if (update_mask[layer] && is_config) begin
                next_row = '0;
                next_row.enabled = s_enable;
                next_row.anchor = s_tick;
                next_row.capacity = s_amount;
                next_row.rate = s_rate;
                next_row.reserve_floor = s_reserve;
                next_row.balance = s_amount;
            end else if (update_mask[layer]) begin
                before_charge[layer] = fresh[layer];
                next_row.anchor = s_tick;
                next_row.balance = fresh[layer];
                if (status == PASS) begin
                    next_row.balance = fresh[layer] - s_amount;
                    if (&current.passed)
                        next_row.saturated[0] = 1'b1;
                    else
                        next_row.passed = current.passed + 1'b1;
                end else if (status == LIMITED && detail[layer]) begin
                    if (&current.blocked)
                        next_row.saturated[1] = 1'b1;
                    else
                        next_row.blocked = current.blocked + 1'b1;
                end
            end
            updated[layer] = next_row;
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            port_bucket <= '0;
            for (integer index = 0; index < GROUPS; index++)
                groups[index] <= '0;
            for (integer index = 0; index < SESSIONS; index++) begin
                sessions[index] <= '0;
                parent[index] <= '0;
                critical_session[index] <= 1'b0;
            end
            result_valid <= 1'b0;
            m_op <= '0;
            m_session <= '0;
            m_group <= '0;
            m_tag <= '0;
            m_status <= '0;
            m_scope <= '0;
            m_detail <= '0;
            m_critical <= 1'b0;
            m_port <= '0;
            m_group_state <= '0;
            m_session_state <= '0;
        end else if (s_ready) begin
            result_valid <= s_valid;
            if (s_valid) begin
                if (update_mask[0])
                    port_bucket <= updated[0];
                if (update_mask[1])
                    groups[group_index] <= updated[1];
                if (update_mask[2]) begin
                    sessions[session_index] <= updated[2];
                    if (s_op == CONFIG_SESSION) begin
                        parent[session_index] <= s_group;
                        critical_session[session_index] <= s_critical;
                    end
                end
                m_op <= s_op;
                m_session <= s_session;
                m_group <= selected_group;
                m_tag <= s_tag;
                m_status <= status;
                m_scope <= scope;
                m_detail <= detail;
                m_critical <= output_critical;
                m_port <= {before_charge[0], updated[0]};
                m_group_state <= {before_charge[1], updated[1]};
                m_session_state <= {before_charge[2], updated[2]};
            end
        end
    end
endmodule

`default_nettype wire
