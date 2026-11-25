    logic seen_reset = 1'b0;
    logic [SUM_W-1:0] summed_bid, summed_market_sell;
    logic [SUM_W-1:0] computed_buy [0:PRICES-1], computed_sell [0:PRICES-1];
    logic [SUM_W-1:0] computed_bid_bin [0:PRICES-1], computed_ask_bin [0:PRICES-1];

    always @* begin : reconstruct_book
        summed_bid = 0;
        summed_market_sell = 0;
        for (integer j = 0; j < SLOTS; j++) begin
            if (live[j]) begin
                if (!side[j])
                    summed_bid = summed_bid + SUM_W'(qty[j]);
                else if (market[j])
                    summed_market_sell = summed_market_sell + SUM_W'(qty[j]);
            end
        end
        for (integer p = 0; p < PRICES; p++) begin
            computed_buy[p] = '0;
            computed_sell[p] = '0;
            computed_bid_bin[p] = '0;
            computed_ask_bin[p] = '0;
            for (integer j = 0; j < SLOTS; j++) begin
                if (live[j] && !side[j] && (market[j] || limit[j] >= PRICE_W'(p)))
                    computed_buy[p] = computed_buy[p] + SUM_W'(qty[j]);
                if (live[j] && side[j] && (market[j] || limit[j] <= PRICE_W'(p)))
                    computed_sell[p] = computed_sell[p] + SUM_W'(qty[j]);
                if (live[j] && !market[j] && limit[j] == PRICE_W'(p)) begin
                    if (side[j])
                        computed_ask_bin[p] = computed_ask_bin[p] + SUM_W'(qty[j]);
                    else
                        computed_bid_bin[p] = computed_bid_bin[p] + SUM_W'(qty[j]);
                end
            end
        end
    end

    wire [13+TAG_W+EPOCH_W+5*ID_W+2*OWNER_W+PRICE_W+3*SUM_W-1:0] formal_payload = {
        m_kind, m_reason, m_last, m_epoch, m_tag, m_first, m_second, m_first_owner, m_second_owner,
        m_side, m_market, m_carry, m_reference_half, m_price, m_quantity, m_buys, m_sells,
        m_count, m_carried, m_expired
    };

    always @(posedge clk) begin
        seen_reset <= 1'b1;
        if (!seen_reset)
            assume(rst);
        if (rst)
            assert(!s_ready && !m_valid);
        if (seen_reset && !rst) begin
            if ($past(!rst && m_valid && !m_ready)) begin
                assert(m_valid);
                assert(formal_payload == $past(formal_payload));
            end
            assert(live_count == $countones(live));
            if (collecting) begin
                assert(bid_total == summed_bid);
                assert(market_sells == summed_market_sell);
                assert(collar_lo <= reference_tick);
                assert(reference_tick <= collar_hi);
                assert(!reference_half || reference_tick < collar_hi);
                assert(collar_hi < PRICE_W'(PRICES));
                for (integer p = 0; p < PRICES; p++) begin
                    assert(bid_bins[p] == computed_bid_bin[p]);
                    assert(ask_bins[p] == computed_ask_bin[p]);
                end
            end
            for (integer i = 0; i < SLOTS; i++) begin
                if (live[i]) begin
                    assert(qty[i] != 0);
                    assert(limit[i] < PRICE_W'(PRICES));
                    assert(!market[i] || (limit[i] == 0 && !carry[i]));
                    if (collecting)
                        assert(age[i] < live_count);
                    for (integer j = i+1; j < SLOTS; j++)
                        if (live[j])
                            assert(age[j] != age[i]);
                end
            end
            if (state == SCAN) begin
                assert(tick <= collar_hi);
                assert(buy_now == computed_buy[TICK_ADDR_W'(tick)]);
                assert(sell_now == computed_sell[TICK_ADDR_W'(tick)]);
            end
            if (state == SUMMARY) begin
                assert(best_tick >= collar_lo && best_tick <= collar_hi);
                assert(best_buy == computed_buy[TICK_ADDR_W'(best_tick)]);
                assert(best_sell == computed_sell[TICK_ADDR_W'(best_tick)]);
                assert(best_volume == (best_buy < best_sell ? best_buy : best_sell));
                for (integer p = 0; p < PRICES; p++) begin : optimality
                    logic [SUM_W-1:0] other_volume, other_gap;
                    logic [PRICE_W:0] other_distance, candidate_tick_x2;
                    other_volume = computed_buy[p] < computed_sell[p] ? computed_buy[p] : computed_sell[p];
                    other_gap = computed_buy[p] < computed_sell[p] ?
                        computed_sell[p]-computed_buy[p] : computed_buy[p]-computed_sell[p];
                    candidate_tick_x2 = {PRICE_W'(p),1'b0};
                    other_distance = candidate_tick_x2 < doubled_reference ?
                        doubled_reference-candidate_tick_x2 : candidate_tick_x2-doubled_reference;
                    if (PRICE_W'(p) >= collar_lo && PRICE_W'(p) <= collar_hi) begin
                        assert(best_volume > other_volume || (best_volume == other_volume &&
                            (best_gap < other_gap || (best_gap == other_gap &&
                             (best_distance < other_distance ||
                              (best_distance == other_distance && best_tick <= PRICE_W'(p)))))));
                    end
                end
            end
            if (state == PAIR) begin
                assert(have_bid && have_ask);
                assert(left_to_trade != 0);
                assert(m_quantity != 0 && m_quantity <= left_to_trade);
                assert(left_to_trade == (
                    computed_buy[TICK_ADDR_W'(best_tick)] < computed_sell[TICK_ADDR_W'(best_tick)]
                    ? computed_buy[TICK_ADDR_W'(best_tick)] : computed_sell[TICK_ADDR_W'(best_tick)]
                ));
                for (integer i = 0; i < SLOTS; i++) begin
                    if (live[i] && !side[i] && (market[i] || limit[i] >= best_tick))
                        assert(!outranks(market[i],market[first_bid],1'b0,
                            limit[i],limit[first_bid],age[i],age[first_bid]));
                    if (live[i] && side[i] && (market[i] || limit[i] <= best_tick))
                        assert(!outranks(market[i],market[first_ask],1'b1,
                            limit[i],limit[first_ask],age[i],age[first_ask]));
                end
            end
            if (state == COMPLETE)
                assert(live_count == 0 && left_to_trade == 0);
            if ($past(!rst && transfer && m_kind == `AU_EV_REJECT))
                assert(live == $past(live) && epoch == $past(epoch));
            cover(transfer && m_kind == `AU_EV_INDICATIVE && m_quantity > 1);
            cover(transfer && m_kind == `AU_EV_TRADE && market[first_bid] && !market[first_ask]);
            cover(transfer && m_kind == `AU_EV_DONE && m_count > 0 && m_carried > 0);
            cover(m_valid && !m_ready && m_kind == `AU_EV_CLEARING);
            cover(transfer && m_kind == `AU_EV_REJECT && m_reason == `AU_ERR_EPOCH);
        end
    end
