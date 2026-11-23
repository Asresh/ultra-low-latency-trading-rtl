// Check compact quote and handshake safety over a bounded arbitrary trace.
module formal_quote_flow(input wire clk, input wire rst);
    localparam integer PRICE_W = 4;
    localparam integer QTY_W = 3;
    localparam integer TIME_W = 4;
    localparam integer SEQ_W = 3;
    localparam integer TAG_W = 3;

    (* anyseq *) wire [TIME_W-1:0] now_tick, query_max_age;
    (* anyseq *) wire cmd_valid, ack_ready, query_valid, result_ready;
    (* anyseq *) wire [2:0] cmd_op;
    (* anyseq *) wire cmd_instrument, query_instrument;
    (* anyseq *) wire [1:0] cmd_venue;
    (* anyseq *) wire [SEQ_W-1:0] cmd_sequence;
    (* anyseq *) wire [PRICE_W-1:0] cmd_bid_price, cmd_ask_price;
    (* anyseq *) wire [QTY_W-1:0] cmd_bid_qty, cmd_ask_qty;
    (* anyseq *) wire [TAG_W-1:0] cmd_tag, query_tag;

    wire cmd_ready, ack_valid, query_ready, result_valid;
    wire [3:0] ack_code, result_code;
    wire [TAG_W-1:0] ack_tag, result_tag;
    wire [2:0] result_state;
    wire [TIME_W-1:0] result_tick, result_bid_age, result_ask_age;
    wire [1:0] result_active, result_stale;
    wire result_bid_valid, result_ask_valid;
    wire [1:0] result_bid_venue, result_ask_venue;
    wire [PRICE_W-1:0] result_bid_price, result_ask_price;
    wire [QTY_W-1:0] result_bid_qty, result_ask_qty;
    wire [SEQ_W-1:0] result_bid_sequence, result_ask_sequence;
    wire signed [PRICE_W:0] result_spread;

    best_quote_selector #(.INSTRUMENTS(1), .VENUES(2), .PRICE_W(PRICE_W),
        .QTY_W(QTY_W), .TIME_W(TIME_W), .SEQ_W(SEQ_W), .TAG_W(TAG_W)) dut (.*);

    wire [15:0] bid = {result_bid_venue, result_bid_price, result_bid_qty,
                      result_bid_age, result_bid_sequence};
    wire [15:0] ask = {result_ask_venue, result_ask_price, result_ask_qty,
                      result_ask_age, result_ask_sequence};
    wire [56:0] result_payload = {
        result_tag, result_code, result_state, result_tick, result_active,
        result_stale, result_bid_valid, result_ask_valid, bid, ask, result_spread
    };
    reg previous = 0;
    always @(posedge clk) begin
        previous <= 1;
        if (rst) begin
            assert(!cmd_ready && !query_ready);
        end
        if (previous && $past(rst)) begin
            assert(!ack_valid);
            assert(!result_valid);
        end
        if (previous && !$past(rst) && !rst) begin
            if ($past(ack_valid && !ack_ready)) begin
                assert(ack_valid);
                if (!ack_ready)
                    assert(!cmd_ready);
                assert({ack_code, ack_tag} == $past({ack_code, ack_tag}));
            end
            if ($past(result_valid && !result_ready)) begin
                assert(result_valid);
                assert(result_payload == $past(result_payload));
            end
            if ($past(cmd_valid && cmd_ready)) begin
                assert(ack_valid);
                assert(ack_tag == $past(cmd_tag));
            end
            if ($past(cmd_ready && !cmd_valid))
                assert(!ack_valid);
        end
        if (previous && !rst && result_valid) begin
            assert(!(result_active & result_stale));
            if (result_code != 0)
                assert({result_active, result_stale, result_bid_valid, result_ask_valid} == 0);
            if (result_bid_valid)
                assert(result_bid_venue < 2 && result_active[result_bid_venue] &&
                       result_bid_price != 0 && result_bid_qty != 0 && !result_bid_age[TIME_W-1]);
            else
                assert(bid == 0);
            if (result_ask_valid)
                assert(result_ask_venue < 2 && result_active[result_ask_venue] &&
                       result_ask_price != 0 && result_ask_qty != 0 && !result_ask_age[TIME_W-1]);
            else
                assert(ask == 0);
            if (result_bid_valid && result_ask_valid)
                assert(result_spread == $signed({1'b0, result_ask_price}) -
                                        $signed({1'b0, result_bid_price}));
            else
                assert(result_spread == 0);
        end
    end
endmodule
