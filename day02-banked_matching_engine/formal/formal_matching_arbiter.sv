`default_nettype none

module formal_matching_arbiter;
    localparam integer PORTS = 3;
    localparam integer SOURCE_W = $clog2(PORTS);
    localparam integer DATA_W = 4;
    localparam integer WIDTH = SOURCE_W + DATA_W;

    (* gclk *) logic clk;
    (* anyseq *) logic rst, m_ready;
    (* anyseq *) logic [PORTS-1:0] s_valid;
    (* anyseq *) logic [PORTS*DATA_W-1:0] arbitrary_data;
    wire [PORTS*WIDTH-1:0] s_payload;
    wire [PORTS-1:0] s_ready;
    wire m_valid;
    wire [WIDTH-1:0] m_payload;
    wire [SOURCE_W-1:0] observed_lane = m_payload[WIDTH-1-:SOURCE_W];
    wire did_transfer = m_valid && m_ready;

    matching_arbiter #(.PORTS(PORTS), .WIDTH(WIDTH)) dut (
        .clk(clk), .rst(rst), .s_valid(s_valid), .s_ready(s_ready),
        .s_payload(s_payload), .m_valid(m_valid), .m_ready(m_ready), .m_payload(m_payload)
    );

    logic past_valid = 1'b0;
    logic [PORTS-1:0] served;
    logic stalled_once;

    always @(posedge clk) begin
        if (!past_valid) assume(rst);
        past_valid <= 1'b1;
        if (rst) begin
            assert(!m_valid && s_ready == '0);
            served <= '0;
            stalled_once <= 1'b0;
        end else begin
            assert($onehot0(s_ready));
            assert((|s_ready) == did_transfer);
            assert((s_ready & ~s_valid) == '0);
            assert(m_valid == (|s_valid));
            if (m_valid) begin
                assert(int'(observed_lane) < PORTS);
                assert(s_valid[observed_lane]);
                assert(m_payload == s_payload[int'(observed_lane)*WIDTH+:WIDTH]);
            end
            if (past_valid && $past(!rst && m_valid && !m_ready)) begin
                assert(m_valid);
                assert(m_payload == $past(m_payload));
            end

            served <= served | s_ready;
            if (m_valid && !m_ready) stalled_once <= 1'b1;
            cover(stalled_once && (&served));
        end
    end

    for (genvar port_index = 0; port_index < PORTS; port_index++) begin : g_port
        localparam logic [SOURCE_W-1:0] SOURCE = SOURCE_W'(port_index);
        logic [SOURCE_W-1:0] bypassed;
        assign s_payload[port_index*WIDTH+:WIDTH] =
            {SOURCE, arbitrary_data[port_index*DATA_W+:DATA_W]};

        always @(posedge clk) begin
            if (rst) begin
                bypassed <= '0;
            end else begin
                if (past_valid && $past(!rst && s_valid[port_index] && !s_ready[port_index])) begin
                    assume(s_valid[port_index]);
                    assume(arbitrary_data[port_index*DATA_W+:DATA_W] ==
                        $past(arbitrary_data[port_index*DATA_W+:DATA_W]));
                end
                if (s_ready[port_index]) assert(observed_lane == SOURCE);
                if (!s_valid[port_index] || s_ready[port_index]) begin
                    bypassed <= '0;
                end else if (did_transfer) begin
                    // Fairness is measured in transferred events rather than wall cycles.
                    assert(int'(bypassed) < PORTS - 1);
                    bypassed <= bypassed + 1'b1;
                end
                cover(s_ready[port_index] && int'(bypassed) == PORTS - 1);
            end
        end
    end
endmodule

`default_nettype wire
