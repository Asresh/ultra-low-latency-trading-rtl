`default_nettype none

module formal_divide(input wire clk);
    localparam integer NUM_W = 7;
    localparam integer DIVISOR = 13;
    localparam integer REM_W = $clog2(DIVISOR);
    (* anyseq *) logic rst;
    (* anyseq *) logic s_valid, m_ready;
    (* anyseq *) logic [NUM_W-1:0] s_value;
    wire s_ready, m_valid;
    wire [NUM_W-1:0] m_quotient;
    wire [REM_W-1:0] m_remainder;

    notional_divide #(.NUM_W(NUM_W), .DIVISOR(DIVISOR)) dut (.*);

    logic past_valid = 0;
    logic calculating = 0;
    logic [3:0] age = '0;
    logic [NUM_W-1:0] expected = '0;
    wire take = s_valid && s_ready;
    wire release_row = m_valid && m_ready;

    always @(posedge clk) begin
        past_valid <= 1;
        if (!past_valid) assume(rst);
        if (rst) begin
            assert(!m_valid && !s_ready);
            calculating <= 0;
            age <= 0;
            expected <= '0;
        end else begin
            if (past_valid) begin
                // The independent model counts time and checks the division identity.
                assert(m_valid == (calculating && age == NUM_W));
                assert(!s_ready || !calculating || release_row);
                if (m_valid) begin
                    assert(m_remainder < DIVISOR);
                    assert(16'(m_quotient) * DIVISOR + 16'(m_remainder) == 16'(expected));
                end
                if ($past(m_valid && !m_ready && !rst)) begin
                    assert(m_valid);
                    assert(m_quotient == $past(m_quotient));
                    assert(m_remainder == $past(m_remainder));
                end
            end
            if (take) begin
                calculating <= 1;
                age <= 0;
                expected <= s_value;
            end else if (release_row) begin
                calculating <= 0;
                age <= 0;
            end else if (calculating && age < NUM_W) begin
                age <= age + 1'b1;
            end
            cover(m_valid && m_quotient == 9 && m_remainder == 10);
            cover(take && release_row);
            cover(past_valid && $past(m_valid && !m_ready && !rst) && release_row);
        end
    end
endmodule

`default_nettype wire
