`default_nettype none

module formal_reserve_book;
    localparam integer SLOTS = 3;
    localparam integer LEVELS = 2;
    localparam integer QTY_W = 3;
    localparam integer OWNER_W = 2;
    localparam integer PRICE_W = $clog2(LEVELS+1);
    localparam integer ID_W = $clog2(SLOTS+1);
    localparam integer CW = 8+ID_W+PRICE_W+3*QTY_W+OWNER_W;
    localparam integer RW = 11+2*ID_W+PRICE_W+5*QTY_W+OWNER_W+2*(ID_W+QTY_W);

    (* gclk *) logic clk;
    (* anyseq *) logic reset, source_valid, sink_ready;
    (* anyseq *) logic [CW-1:0] command;
    wire source_ready, sink_valid;
    wire [RW-1:0] result;
    logic running = 1'b0;

    reserve_order_book #(
        .PRICE_LEVELS(LEVELS), .ORDER_SLOTS(SLOTS), .QTY_W(QTY_W), .OWNER_W(OWNER_W)
    ) book (
        .clk(clk), .rst(reset), .s_valid(source_valid), .s_ready(source_ready),
        .s_command(command), .m_valid(sink_valid), .m_ready(sink_ready), .m_result(result)
    );

    // Reset is synchronous. Keep later reset edges unconstrained.
    always @(posedge clk) begin
        if (!running)
            assume(reset);
        running <= 1'b1;
        if (reset) begin
            assert(!source_ready);
            assert(!sink_valid);
        end else begin
            assert(source_ready != sink_valid);
        end
    end
endmodule

`default_nettype wire
