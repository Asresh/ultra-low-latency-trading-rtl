`default_nettype none

module formal_matching_fifo;
    localparam integer WIDTH = 5;
    localparam integer DEPTH = 3;
    localparam integer COUNT_W = $clog2(DEPTH + 1);

    (* gclk *) logic clk;
    (* anyseq *) logic rst, s_valid, m_ready;
    (* anyseq *) logic [WIDTH-1:0] s_payload;
    wire s_ready, m_valid;
    wire [WIDTH-1:0] m_payload;

    matching_fifo #(.WIDTH(WIDTH), .DEPTH(DEPTH)) dut (
        .clk(clk), .rst(rst), .s_valid(s_valid), .s_ready(s_ready), .s_payload(s_payload),
        .m_valid(m_valid), .m_ready(m_ready), .m_payload(m_payload)
    );

    wire pop = m_valid && m_ready;
    wire push = s_valid && s_ready;
    logic past_valid = 1'b0;
    logic [WIDTH*DEPTH-1:0] queue_ref, next_queue;
    logic [COUNT_W-1:0] count_ref;
    // Keep consecutive occupancy history independent of DUT pointers.
    logic reached_capacity;
    logic saw_both;
    logic [2:0] admitted, transmitted;

    always_comb begin
        next_queue = pop ? (queue_ref >> WIDTH) : queue_ref;
        if (push) next_queue[(int'(count_ref) - int'(pop))*WIDTH+:WIDTH] = s_payload;
    end

    always @(posedge clk) begin
        if (!past_valid) assume(rst);
        past_valid <= 1'b1;
        if (rst) begin
            assert(!s_ready && !m_valid);
            queue_ref <= '0;
            count_ref <= '0;
            reached_capacity <= 1'b0;
            saw_both <= 1'b0;
            admitted <= '0;
            transmitted <= '0;
        end else begin
            assert(count_ref <= COUNT_W'(DEPTH));
            assert(s_ready == (count_ref < COUNT_W'(DEPTH)));
            assert(m_valid == (count_ref != 0));
            if (m_valid) assert(m_payload == queue_ref[WIDTH-1:0]);
            if (past_valid && $past(!rst && m_valid && !m_ready)) begin
                assert(m_valid);
                assert(m_payload == $past(m_payload));
            end

            queue_ref <= next_queue;
            case ({push, pop})
                2'b10: count_ref <= count_ref + 1'b1;
                2'b01: count_ref <= count_ref - 1'b1;
                default: count_ref <= count_ref;
            endcase
            if (!s_ready) reached_capacity <= 1'b1;
            if (push && pop) saw_both <= 1'b1;
            if (push && admitted < 7) admitted <= admitted + 1'b1;
            if (pop && transmitted < 7) transmitted <= transmitted + 1'b1;
            cover(reached_capacity && count_ref == 0);
            cover(saw_both && admitted >= 6 && transmitted >= 5);
        end
    end
endmodule

`default_nettype wire
