`default_nettype none
`include "market_codes.svh"

module formal_quote_policy;
    localparam integer LEVELS = 7;
    localparam integer PW = $clog2(LEVELS+1);
    localparam integer QW = 5;
    localparam integer DW = 7;
    localparam integer SIZE_CAP = 17;

    (* anyconst *) wire live, bid_present, ask_present;
    (* anyconst *) wire [PW-1:0] bid_price, ask_price;
    (* anyconst *) wire [DW-1:0] bid_size, ask_size;
    wire active;
    wire pressure_bid, pressure_ask;
    wire [PW-1:0] quote_bid, quote_ask;
    wire [QW-1:0] quote_size;
    wire [2:0] reason;

    passive_quote_policy #(
        .LEVELS(LEVELS), .QTY_W(QW), .DEPTH_W(DW), .BASE_QTY(SIZE_CAP),
        .HALF_SPREAD(1), .SKEW_TICKS(3), .MIN_MARKET_SPREAD(2), .IMBALANCE_Q8(64)
    ) dut (.*);

    wire [DW:0] sum_depth = {1'b0, bid_size} + {1'b0, ask_size};
    wire [DW:0] bid_difference = {1'b0, bid_size} - {1'b0, ask_size};
    wire [DW:0] ask_difference = {1'b0, ask_size} - {1'b0, bid_size};
    wire buy_threshold = ({bid_difference, 2'b00} >= {{2{1'b0}}, sum_depth});
    wire sell_threshold = ({ask_difference, 2'b00} >= {{2{1'b0}}, sum_depth});

    always @* begin
        assume(bid_price < PW'(LEVELS));
        assume(ask_price < PW'(LEVELS));
        assert(!(pressure_bid && pressure_ask));
        assert(pressure_bid == (live && bid_present && ask_present && bid_size > ask_size && buy_threshold));
        assert(pressure_ask == (live && bid_present && ask_present && ask_size > bid_size && sell_threshold));
        if (active) begin
            assert(live && bid_present && ask_present);
            assert(quote_bid < quote_ask && quote_ask < PW'(LEVELS));
            assert(quote_bid < ask_price && quote_ask > bid_price);
            assert(quote_size > 0 && quote_size <= QW'(SIZE_CAP));
            assert(DW'(quote_size) <= bid_size && DW'(quote_size) <= ask_size);
            assert(reason == `QR_QUOTING);
            assert(ask_price-bid_price >= PW'(2));
        end else begin
            assert(quote_bid == 0 && quote_ask == 0 && quote_size == 0);
            assert(reason != `QR_QUOTING);
        end
        if (!live)
            assert(!active && reason == `QR_INACTIVE);
        else if (!bid_present || !ask_present)
            assert(!active && reason == `QR_NO_SIDES);
        else if (bid_price >= ask_price)
            assert(!active && reason == `QR_CROSSED);
        else if (ask_price-bid_price < PW'(2))
            assert(!active && reason == `QR_SPREAD);
        else if (bid_size == 0 || ask_size == 0)
            assert(!active && reason == `QR_ZERO_SIZE);
        else
            assert(active);
        cover(active && pressure_bid);
        cover(active && pressure_ask);
        cover(active && !pressure_bid && !pressure_ask);
        cover(live && reason == `QR_CROSSED);
        cover(live && reason == `QR_ZERO_SIZE);
    end
endmodule

`default_nettype wire
