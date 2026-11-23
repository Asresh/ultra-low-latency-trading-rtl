`default_nettype none

// Check observable safety and handshakes on a small instance with all input
// combinations available to the solver. Stability excludes reset because
// reset explicitly cancels a response which has not transferred.
module formal_limiter (
    input logic clk,
    input logic rst,
    input logic s_valid,
    input logic [1:0] s_op,
    input logic [1:0] s_key,
    input logic [3:0] s_tick,
    input logic [3:0] s_amount,
    input logic [3:0] s_rate,
    input logic s_enable,
    input logic [3:0] s_tag,
    input logic m_ready
);
    localparam logic [3:0] PASS = 4'd1;
    localparam logic [3:0] LIMITED = 4'd2;
    localparam logic [3:0] BAD_KEY = 4'd3;
    localparam logic [3:0] BAD_TICK = 4'd6;

    logic s_ready;
    logic m_valid;
    logic [1:0] m_op;
    logic [1:0] m_key;
    logic [3:0] m_tag;
    logic [3:0] m_status;
    logic m_enabled;
    logic [3:0] m_anchor;
    logic [3:0] m_capacity;
    logic [3:0] m_rate;
    logic [3:0] m_before;
    logic [3:0] m_balance;
    logic [2:0] m_passed;
    logic [2:0] m_limited;
    logic [2:0] m_charged;
    logic [2:0] m_saturated;

    message_rate_limiter #(
        .KEYS(3), .KEY_W(2), .TICK_W(4), .TOKEN_W(4), .COUNT_W(3), .TAG_W(4)
    ) dut (.*);

    logic history_valid = 1'b0;
    logic pending_charge;
    logic [3:0] issue_amount;
    logic [45:0] snapshot;
    logic [12:0] result_key;
    logic [20:0] bucket_report;

    assign result_key = {m_op, m_key, m_tag, m_status, m_enabled};
    assign bucket_report = {m_anchor, m_capacity, m_rate, m_before, m_balance, m_valid};
    assign snapshot = {result_key, bucket_report, m_passed, m_limited, m_charged, m_saturated};

    always_ff @(posedge clk) begin
        if (!history_valid)
            assume(rst);
        history_valid <= 1'b1;
        if (rst) begin
            pending_charge <= 1'b0;
            issue_amount <= 0;
        end else if (s_ready) begin
            pending_charge <= s_valid && s_op == 2'd1;
            issue_amount <= s_amount;
        end

        if (history_valid) begin
            assert(s_ready == (!rst && (!m_valid || m_ready)));
            if ($past(rst))
                assert(!m_valid);
            if (!$past(rst) && $past(m_valid) && !$past(m_ready))
                assert(snapshot == $past(snapshot));
            if (!$past(rst) && $past(s_ready)) begin
                assert(m_valid == $past(s_valid));
                if (m_valid) begin
                    assert(m_op == $past(s_op));
                    assert(m_key == $past(s_key));
                    assert(m_tag == $past(s_tag));
                end
            end
            if (m_valid) begin
                assert(m_status < 7);
                assert(m_balance <= m_capacity);
                assert(m_rate <= m_capacity);
                if (!m_enabled) begin
                    assert(m_capacity == 0);
                    assert(m_rate == 0);
                    assert(m_balance == 0);
                    assert(m_saturated == 0);
                end
                if (m_saturated[0])
                    assert(&m_passed);
                if (m_saturated[1])
                    assert(&m_limited);
                if (m_saturated[2])
                    assert(&m_charged);
                if (m_status == PASS) begin
                    assert(pending_charge && m_enabled);
                    assert(issue_amount > 0);
                    assert(m_before >= m_balance);
                    assert(m_before - m_balance == issue_amount);
                end
                if (m_status == LIMITED) begin
                    assert(pending_charge && m_enabled);
                    assert(m_before < issue_amount);
                    assert(m_balance == m_before);
                end
                if (m_status == BAD_KEY)
                    assert(m_key == 3);
            end
`ifdef COVER
            cover(m_valid && m_status == BAD_TICK);
            cover(m_valid && m_status == PASS && m_saturated[2]);
            cover(m_valid && m_status == LIMITED && !m_ready);
            cover(m_valid && s_valid && s_ready);
`endif
        end
    end
endmodule

`default_nettype wire
