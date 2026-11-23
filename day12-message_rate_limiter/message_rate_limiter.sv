`timescale 1ns/1ps
`default_nettype none

// Apply one command atomically and publish a complete post-command snapshot.
// A full response slot stalls all requests, so accounting can never get ahead
// of the result stream. Requests for different keys share only that slot.
module message_rate_limiter #(
    parameter integer KEYS = 5,
    parameter integer KEY_W = (KEYS < 2) ? 1 : $clog2(KEYS),
    parameter integer TICK_W = 12,
    parameter integer TOKEN_W = 12,
    parameter integer COUNT_W = 16,
    parameter integer TAG_W = 16
) (
    input  logic clk,
    input  logic rst,

    input  logic s_valid,
    output logic s_ready,
    input  logic [1:0] s_op,
    input  logic [KEY_W-1:0] s_key,
    input  logic [TICK_W-1:0] s_tick,
    input  logic [TOKEN_W-1:0] s_amount,
    input  logic [TOKEN_W-1:0] s_rate,
    input  logic s_enable,
    input  logic [TAG_W-1:0] s_tag,

    output logic m_valid,
    input  logic m_ready,
    output logic [1:0] m_op,
    output logic [KEY_W-1:0] m_key,
    output logic [TAG_W-1:0] m_tag,
    output logic [3:0] m_status,
    output logic m_enabled,
    output logic [TICK_W-1:0] m_anchor,
    output logic [TOKEN_W-1:0] m_capacity,
    output logic [TOKEN_W-1:0] m_rate,
    output logic [TOKEN_W-1:0] m_before,
    output logic [TOKEN_W-1:0] m_balance,
    output logic [COUNT_W-1:0] m_passed,
    output logic [COUNT_W-1:0] m_limited,
    output logic [COUNT_W-1:0] m_charged,
    output logic [2:0] m_saturated
);
    localparam logic [1:0] CONFIG = 2'd0;
    localparam logic [1:0] OFFER = 2'd1;
    localparam logic [1:0] INSPECT = 2'd2;
    localparam logic [1:0] CLEAR = 2'd3;

    localparam logic [3:0] OK = 4'd0;
    localparam logic [3:0] PASS = 4'd1;
    localparam logic [3:0] LIMITED = 4'd2;
    localparam logic [3:0] BAD_KEY = 4'd3;
    localparam logic [3:0] BAD_FIELDS = 4'd4;
    localparam logic [3:0] DISABLED = 4'd5;
    localparam logic [3:0] BAD_TICK = 4'd6;

    typedef struct packed {
        logic enabled;
        logic [TICK_W-1:0] anchor;
        logic [TOKEN_W-1:0] capacity;
        logic [TOKEN_W-1:0] rate;
        logic [TOKEN_W-1:0] balance;
        logic [COUNT_W-1:0] passed;
        logic [COUNT_W-1:0] limited;
        logic [COUNT_W-1:0] charged;
        logic [2:0] saturated;
    } bucket_t;

    bucket_t table_data [0:KEYS-1];
    bucket_t current;
    bucket_t next_bucket;
    logic chronological;
    logic [TOKEN_W-1:0] available;
    logic [TOKEN_W-1:0] before_balance;
    logic [3:0] status;
    logic write_state;
    logic key_valid;

    localparam integer ADD_W = ((TOKEN_W > COUNT_W) ? TOKEN_W : COUNT_W) + 1;

    // Add full-width unsigned inputs, then clip to the exported counter width.
    // The top result bit tells the caller that information was lost.
    function automatic logic [COUNT_W:0] add_count(
        input logic [COUNT_W-1:0] value,
        input logic [TOKEN_W-1:0] delta
    );
        logic [ADD_W-1:0] total;
        begin
            total = ADD_W'(value) + ADD_W'(delta);
            if (|total[ADD_W-1:COUNT_W])
                add_count = {1'b1, {COUNT_W{1'b1}}};
            else
                add_count = {1'b0, total[COUNT_W-1:0]};
        end
    endfunction

    // A zero-cost offer is invalid. Auxiliary fields must be clear for
    // non-configuration commands so the same wire image has one meaning.
    function automatic logic valid_fields(
        input logic [1:0] op,
        input logic [TOKEN_W-1:0] amount,
        input logic [TOKEN_W-1:0] rate,
        input logic enable
    );
        case (op)
            CONFIG: valid_fields = enable
                                 ? (amount != 0 && rate <= amount)
                                 : (amount == 0 && rate == 0);
            OFFER: valid_fields = !enable && rate == 0 && amount != 0;
            INSPECT, CLEAR: valid_fields = !enable && rate == 0 && amount == 0;
            default: valid_fields = 1'b0;
        endcase
    endfunction

    assign key_valid = (int'(s_key) < KEYS);
    assign current = key_valid ? table_data[s_key] : '0;
    assign s_ready = !rst && (!m_valid || m_ready);

    rate_refill #(.TICK_W(TICK_W), .TOKEN_W(TOKEN_W)) refill_bucket (
        .tick(s_tick),
        .anchor(current.anchor),
        .balance(current.balance),
        .capacity(current.capacity),
        .rate(current.rate),
        .chronological(chronological),
        .available(available)
    );

    logic [COUNT_W:0] passed_next, limited_next, charged_next;

    assign passed_next = add_count(current.passed, TOKEN_W'(1));
    assign limited_next = add_count(current.limited, TOKEN_W'(1));
    assign charged_next = add_count(current.charged, s_amount);

    always_comb begin
        next_bucket = current;
        before_balance = current.balance;
        status = OK;
        write_state = 1'b0;
        if (!key_valid) begin
            status = BAD_KEY;
        end else if (!valid_fields(s_op, s_amount, s_rate, s_enable)) begin
            status = BAD_FIELDS;
        end else if (s_op == CONFIG) begin
            // Configuration creates a full bucket and restarts its counters.
            next_bucket = '0;
            next_bucket.enabled = s_enable;
            next_bucket.anchor = s_tick;
            next_bucket.capacity = s_amount;
            next_bucket.rate = s_rate;
            next_bucket.balance = s_amount;
            write_state = 1'b1;
        end else if (!current.enabled && s_op == OFFER) begin
            status = DISABLED;
        end else if (current.enabled && !chronological) begin
            status = BAD_TICK;
        end else begin
            write_state = 1'b1;
            if (current.enabled) begin
                next_bucket.anchor = s_tick;
                before_balance = available;
                next_bucket.balance = available;
            end
            case (s_op)
                OFFER: if (s_amount <= available) begin
                    status = PASS;
                    next_bucket.balance = available - s_amount;
                    next_bucket.passed = passed_next[COUNT_W-1:0];
                    next_bucket.charged = charged_next[COUNT_W-1:0];
                    next_bucket.saturated[0] = current.saturated[0] | passed_next[COUNT_W];
                    next_bucket.saturated[2] = current.saturated[2] | charged_next[COUNT_W];
                end else begin
                    status = LIMITED;
                    next_bucket.limited = limited_next[COUNT_W-1:0];
                    next_bucket.saturated[1] = current.saturated[1] | limited_next[COUNT_W];
                end
                CLEAR: begin
                    next_bucket.passed = '0;
                    next_bucket.limited = '0;
                    next_bucket.charged = '0;
                    next_bucket.saturated = '0;
                end
                default: ;
            endcase
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            for (integer key = 0; key < KEYS; key = key + 1)
                table_data[key] <= '0;
            m_valid <= 1'b0;
            m_op <= '0;
            m_key <= '0;
            m_tag <= '0;
            m_status <= '0;
            m_enabled <= 1'b0;
            m_anchor <= '0;
            m_capacity <= '0;
            m_rate <= '0;
            m_before <= '0;
            m_balance <= '0;
            m_passed <= '0;
            m_limited <= '0;
            m_charged <= '0;
            m_saturated <= '0;
        end else if (s_ready) begin
            // Replacing a consumed response is legal on the same edge.
            m_valid <= s_valid;
            if (s_valid) begin
                if (write_state)
                    table_data[s_key] <= next_bucket;
                m_op <= s_op;
                m_key <= s_key;
                m_tag <= s_tag;
                m_status <= status;
                m_enabled <= next_bucket.enabled;
                m_anchor <= next_bucket.anchor;
                m_capacity <= next_bucket.capacity;
                m_rate <= next_bucket.rate;
                m_before <= before_balance;
                m_balance <= next_bucket.balance;
                m_passed <= next_bucket.passed;
                m_limited <= next_bucket.limited;
                m_charged <= next_bucket.charged;
                m_saturated <= next_bucket.saturated;
            end
        end
    end
endmodule

`default_nettype wire
