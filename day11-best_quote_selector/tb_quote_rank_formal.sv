// The tournament tree must agree with a complete scan for unconstrained data.
module formal_quote_rank;
    localparam integer VENUES = 5;
    localparam integer PRICE_W = 4;
    localparam integer PAYLOAD_W = 7;
    (* anyconst *) wire [VENUES-1:0] candidate_valid;
    (* anyconst *) wire [PRICE_W*VENUES-1:0] candidate_price;
    (* anyconst *) wire [PAYLOAD_W*VENUES-1:0] candidate_payload;
    wire bid_valid, ask_valid;
    wire [PRICE_W-1:0] bid_price, ask_price;
    wire [PAYLOAD_W-1:0] bid_payload, ask_payload;
    wire [2:0] bid_venue, ask_venue;

    quote_reduce_side #(.VENUES(VENUES), .PRICE_W(PRICE_W),
        .PAYLOAD_W(PAYLOAD_W), .HIGH_WINS(1'b1)) bid_rank (
        .candidate_valid(candidate_valid), .candidate_price(candidate_price),
        .candidate_payload(candidate_payload), .winner_valid(bid_valid),
        .winner_price(bid_price), .winner_payload(bid_payload), .winner_venue(bid_venue)
    );
    quote_reduce_side #(.VENUES(VENUES), .PRICE_W(PRICE_W),
        .PAYLOAD_W(PAYLOAD_W), .HIGH_WINS(1'b0)) ask_rank (
        .candidate_valid(candidate_valid), .candidate_price(candidate_price),
        .candidate_payload(candidate_payload), .winner_valid(ask_valid),
        .winner_price(ask_price), .winner_payload(ask_payload), .winner_venue(ask_venue)
    );

    always @* begin
        assert(bid_valid == |candidate_valid);
        assert(ask_valid == |candidate_valid);
        if (bid_valid) begin
            assert(bid_venue < VENUES && ask_venue < VENUES);
            assert(candidate_valid[bid_venue] && candidate_valid[ask_venue]);
            assert(bid_price == candidate_price[bid_venue*PRICE_W +: PRICE_W]);
            assert(ask_price == candidate_price[ask_venue*PRICE_W +: PRICE_W]);
            assert(bid_payload == candidate_payload[bid_venue*PAYLOAD_W +: PAYLOAD_W]);
            assert(ask_payload == candidate_payload[ask_venue*PAYLOAD_W +: PAYLOAD_W]);
            for (integer i = 0; i < VENUES; i++) begin
                if (candidate_valid[i]) begin
                    assert(bid_price >= candidate_price[i*PRICE_W +: PRICE_W]);
                    assert(ask_price <= candidate_price[i*PRICE_W +: PRICE_W]);
                    if (bid_price == candidate_price[i*PRICE_W +: PRICE_W])
                        assert(bid_venue <= i);
                    if (ask_price == candidate_price[i*PRICE_W +: PRICE_W])
                        assert(ask_venue <= i);
                end
            end
        end else begin
            assert({bid_price, bid_venue, bid_payload} == 0);
            assert({ask_price, ask_venue, ask_payload} == 0);
        end
    end
endmodule
