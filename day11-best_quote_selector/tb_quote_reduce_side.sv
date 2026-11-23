`timescale 1ns/1ps

module tb_quote_reduce_side;
    localparam integer VENUES = 5;
    localparam integer PRICE_W = 4;
    localparam integer PAYLOAD_W = 9;
    localparam integer VENUE_W = 3;
    reg [VENUES-1:0] candidate_valid;
    reg [VENUES*PRICE_W-1:0] candidate_price;
    reg [VENUES*PAYLOAD_W-1:0] candidate_payload;
    wire high_valid, low_valid;
    wire [PRICE_W-1:0] high_price, low_price;
    wire [PAYLOAD_W-1:0] high_payload, low_payload;
    wire [VENUE_W-1:0] high_venue, low_venue;

    quote_reduce_side #(.VENUES(VENUES), .PRICE_W(PRICE_W),
        .PAYLOAD_W(PAYLOAD_W), .VENUE_W(VENUE_W), .HIGH_WINS(1)) high_dut (
        .candidate_valid(candidate_valid), .candidate_price(candidate_price),
        .candidate_payload(candidate_payload), .winner_valid(high_valid),
        .winner_price(high_price), .winner_payload(high_payload), .winner_venue(high_venue)
    );
    quote_reduce_side #(.VENUES(VENUES), .PRICE_W(PRICE_W),
        .PAYLOAD_W(PAYLOAD_W), .VENUE_W(VENUE_W), .HIGH_WINS(0)) low_dut (
        .candidate_valid(candidate_valid), .candidate_price(candidate_price),
        .candidate_payload(candidate_payload), .winner_valid(low_valid),
        .winner_price(low_price), .winner_payload(low_payload), .winner_venue(low_venue)
    );

    // Sequentially scan the candidates, retaining the first equal quote.
    // This is deliberately different from the RTL's parallel reduction tree.
    function automatic integer expected_index(input bit high_wins);
        integer index;
        integer best;
        reg [PRICE_W-1:0] price, best_price;
        begin
            best = -1;
            best_price = '0;
            for (index = 0; index < VENUES; index++) begin
                price = candidate_price[index*PRICE_W +: PRICE_W];
                if (candidate_valid[index] && (best < 0 ||
                    (high_wins && price > best_price) ||
                    (!high_wins && price < best_price))) begin
                    best = index;
                    best_price = price;
                end
            end
            expected_index = best;
        end
    endfunction

    // Validate both arithmetic and forwarding, including canonical empty output.
    task automatic verify_side(
        input bit high_wins, input logic valid, input logic [PRICE_W-1:0] price,
        input logic [PAYLOAD_W-1:0] payload, input logic [VENUE_W-1:0] venue
    );
        integer index;
        begin
            index = expected_index(high_wins);
            if (index < 0) begin
                if ({valid, price, payload, venue} !== 0)
                    $fatal(1, "empty tree emitted a candidate");
            end else begin
                if (valid !== 1'b1 || venue !== VENUE_W'(index) ||
                    price !== candidate_price[index*PRICE_W +: PRICE_W] ||
                    payload !== candidate_payload[index*PAYLOAD_W +: PAYLOAD_W])
                    $fatal(1, "tree differs from scan at mask=%b index=%0d", candidate_valid, index);
            end
        end
    endtask

    integer tested;
    initial begin
        tested = 0;
        for (integer mask = 0; mask < 1 << VENUES; mask++) begin
            candidate_valid = VENUES'(mask);
            // Enumerate every combination of four prices across all five leaves.
            // Lots of ties guarantee the masks exercise the leftmost winner.
            for (integer permutation = 0; permutation < 4**VENUES; permutation++) begin
                for (integer slot = 0; slot < VENUES; slot++) begin
                    candidate_price[slot*PRICE_W +: PRICE_W] =
                        PRICE_W'(3 * ((permutation >> (2*slot)) & 3));
                    candidate_payload[slot*PAYLOAD_W +: PAYLOAD_W] =
                        PAYLOAD_W'(slot * 47 + permutation);
                end
                #1;
                verify_side(1, high_valid, high_price, high_payload, high_venue);
                verify_side(0, low_valid, low_price, low_payload, low_venue);
                tested++;
            end
        end
        $display("PASS rank tree exhaustive masks and prices: %0d", tested);
        $finish;
    end
endmodule
