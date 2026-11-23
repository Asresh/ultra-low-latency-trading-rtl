`timescale 1ns/1ps
`default_nettype none
`include "market_codes.svh"

module policy_trial #(
    parameter integer LEVELS = 16,
    parameter integer BASE = 17,
    parameter integer HALF = 1,
    parameter integer SKEW = 1,
    parameter integer MINIMUM = 2,
    parameter integer THRESHOLD = 64,
    localparam integer PW = $clog2(LEVELS+1)
) (
    output bit finished
);
    localparam integer QW = 6;
    localparam integer DW = 10;
    localparam integer MAX_DEPTH = (1<<DW)-1;
    logic live, bid_present, ask_present;
    logic [PW-1:0] bid_price, ask_price;
    logic [DW-1:0] bid_size, ask_size;
    wire active, pressure_bid, pressure_ask;
    wire [PW-1:0] quote_bid, quote_ask;
    wire [QW-1:0] quote_size;
    wire [2:0] reason;

    passive_quote_policy #(
        .LEVELS(LEVELS), .QTY_W(QW), .DEPTH_W(DW), .BASE_QTY(BASE),
        .HALF_SPREAD(HALF), .SKEW_TICKS(SKEW), .MIN_MARKET_SPREAD(MINIMUM), .IMBALANCE_Q8(THRESHOLD)
    ) dut (.*);

    integer vectors = 0;
    integer observed [0:5];
    integer up_cases = 0, down_cases = 0, tie_threshold = 0, clipped_cases = 0;
    integer QtyValues [0:8];

    // Bound a reference quote price to the permitted tick range.
    function automatic integer clamp(input integer price);
        if (price < 0)
            clamp = 0;
        else if (price >= LEVELS)
            clamp = LEVELS-1;
        else
            clamp = price;
    endfunction

    // Apply a quote-policy case and compare its price, size, and rejection reason.
    task automatic trial(
        input bit enabled, input bit present_bid, input bit present_ask,
        input integer px_bid, input integer px_ask,
        input integer qty_bid, input integer qty_ask
    );
        integer skew_model, expected_bid, expected_ask, expected_qty;
        integer distance, scaled_limit, signed_difference;
        bit up, down, expected_active;
        logic [2:0] expected_reason;
        live = enabled;
        bid_present = present_bid;
        ask_present = present_ask;
        bid_price = PW'(px_bid);
        ask_price = PW'(px_ask);
        bid_size = DW'(qty_bid);
        ask_size = DW'(qty_ask);
        expected_reason = `QR_QUOTING;
        expected_active = 1'b0;
        expected_bid = 0;
        expected_ask = 0;
        expected_qty = 0;

        signed_difference = qty_bid-qty_ask;
        distance = ((signed_difference < 0) ? -signed_difference : signed_difference)*256;
        scaled_limit = (qty_bid + qty_ask)*THRESHOLD;
        up = enabled && present_bid && present_ask && signed_difference > 0 && distance >= scaled_limit;
        down = enabled && present_bid && present_ask && signed_difference < 0 && distance >= scaled_limit;
        skew_model = int'(up)*SKEW - int'(down)*SKEW;
        if (!enabled) begin
            expected_reason = `QR_INACTIVE;
        end else if (!present_bid || !present_ask) begin
            expected_reason = `QR_NO_SIDES;
        end else if (px_bid >= px_ask) begin
            expected_reason = `QR_CROSSED;
        end else begin
            expected_bid = clamp((px_bid+px_ask)/2 + skew_model - HALF);
            expected_ask = clamp((px_bid+px_ask+1)/2 + skew_model + HALF);
            if (expected_bid >= px_ask) begin
                expected_bid = px_ask-1;
                clipped_cases++;
            end
            if (expected_ask <= px_bid) begin
                expected_ask = px_bid+1;
                clipped_cases++;
            end
            expected_qty = (qty_bid < qty_ask) ? qty_bid : qty_ask;
            if (expected_qty > BASE)
                expected_qty = BASE;
            if ((px_ask-px_bid) < MINIMUM || expected_bid >= expected_ask)
                expected_reason = `QR_SPREAD;
            else if (expected_qty == 0)
                expected_reason = `QR_ZERO_SIZE;
            else
                expected_active = 1'b1;
            if (!expected_active) begin
                expected_bid = 0;
                expected_ask = 0;
                expected_qty = 0;
            end
        end

        #1;
        if ({active, quote_bid, quote_ask, quote_size, reason, pressure_bid, pressure_ask}
            !== {expected_active, PW'(expected_bid), PW'(expected_ask), QW'(expected_qty),
                 expected_reason, up, down})
            $fatal(1, "policy levels=%0d threshold=%0d input bbo=%0d:%0d/%0d:%0d expected=%0b %0d,%0d x%0d r=%0d got=%0b %0d,%0d x%0d r=%0d",
                LEVELS, THRESHOLD, px_bid, qty_bid, px_ask, qty_ask,
                expected_active, expected_bid, expected_ask, expected_qty, expected_reason,
                active, quote_bid, quote_ask, quote_size, reason);
        if (active && (quote_bid >= quote_ask || quote_bid >= ask_price || quote_ask <= bid_price))
            $fatal(1, "active intent can cross the input BBO");
        vectors++;
        observed[expected_reason]++;
        up_cases += int'(up);
        down_cases += int'(down);
        if ((up || down) && distance == scaled_limit)
            tie_threshold++;
    endtask

    initial begin
        QtyValues[0] = 0;
        QtyValues[1] = 1;
        QtyValues[2] = 3;
        QtyValues[3] = 5;
        QtyValues[4] = 7;
        QtyValues[5] = 16;
        QtyValues[6] = 32;
        QtyValues[7] = 512;
        QtyValues[8] = MAX_DEPTH;
        for (integer kind = 0; kind < 6; kind++)
            observed[kind] = 0;
        trial(0, 1, 1, 0, LEVELS-1, 8, 1);
        trial(1, 0, 1, 0, LEVELS-1, 8, 1);
        trial(1, 1, 0, 0, LEVELS-1, 1, 8);
        trial(1, 0, 0, 0, LEVELS-1, MAX_DEPTH, MAX_DEPTH);
        for (integer bid = 0; bid < LEVELS; bid++) begin
            for (integer ask = 0; ask < LEVELS; ask++) begin
                for (integer bsize = 0; bsize < 9; bsize++) begin
                    for (integer asize = 0; asize < 9; asize++)
                        trial(1, 1, 1, bid, ask, QtyValues[bsize], QtyValues[asize]);
                end
            end
        end
        if (observed[`QR_INACTIVE] == 0 || observed[`QR_NO_SIDES] == 0 ||
            observed[`QR_CROSSED] == 0 || observed[`QR_ZERO_SIZE] == 0)
            $fatal(1, "missing policy fence cases");
        if ((BASE > 0 && observed[`QR_QUOTING] == 0) ||
            (MINIMUM > 1 && observed[`QR_SPREAD] == 0) || up_cases == 0 || down_cases == 0)
            $fatal(1, "missing pressure/spread/enable coverage");
        if (THRESHOLD == 64 && tie_threshold == 0)
            $fatal(1, "missing exact Q0.8 threshold case");
        $display("PASS policy levels=%0d base=%0d imbalance=%0d vectors=%0d boundary=%0d clamps=%0d",
            LEVELS, BASE, THRESHOLD, vectors, tie_threshold, clipped_cases);
        finished = 1'b1;
    end
endmodule

module tb_passive_quote_policy;
    wire [3:0] done;
    policy_trial standard (.finished(done[0]));
    policy_trial #(.LEVELS(7), .SKEW(3), .THRESHOLD(96), .MINIMUM(3)) asym (.finished(done[1]));
    policy_trial #(.LEVELS(3), .THRESHOLD(256), .MINIMUM(1)) one_sided (.finished(done[2]));
    policy_trial #(.LEVELS(5), .BASE(0), .THRESHOLD(0), .MINIMUM(1)) disabled (.finished(done[3]));
    initial begin
        wait (&done);
        $finish;
    end
    initial begin
        #1000000;
        $fatal(1, "policy bench timed out");
    end
endmodule

`default_nettype wire
