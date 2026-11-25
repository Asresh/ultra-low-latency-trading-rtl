    logic past_valid = 1'b0;
    always @(posedge clk) begin
        past_valid <= 1'b1;
        if (!rst && past_valid && $past(!rst)) begin
            assert(live_count == $countones(live));
            if ($past(m_valid && !m_ready)) begin
                assert(m_valid);
                assert(m_result == $past(m_result));
            end
            if (m_valid && event_kind == `RM_EV_FILL) begin
                assert(event_qty != '0 && event_qty <= remaining);
                assert(event_qty <= visible[maker]);
                assert(stp == `RM_STP_NONE || owner[maker] != account);
            end
            if (m_valid && operation == `RM_NEW && tif == `RM_FOK && !first_event)
                assert(event_kind == `RM_EV_FILL || event_kind == `RM_EV_STP_MAKER);
            if ($past(m_valid && m_ready && event_kind == `RM_EV_REJECT))
                assert(live == $past(live));
            if ($past(m_valid && m_ready && event_kind == `RM_EV_MASS_PREVIEW))
                assert(live == $past(live));
            if (m_valid && bulk_row) begin
                assert(owner[bulk_slot] == account);
                assert(live[bulk_slot]);
                assert(event_count < ID_W'(ORDER_SLOTS));
                for (integer earlier = 0; earlier < ORDER_SLOTS; earlier = earlier + 1)
                    if (live[earlier] && owner[earlier] == account &&
                        in_scope(stp, side[earlier], command_side, price[earlier], limit_price) &&
                        (tif != `RM_MASS_PREVIEW || age[earlier] >= bulk_cursor))
                        assert(age[bulk_slot] <= age[earlier]);
            end
            if (m_valid && operation == `RM_MANAGE)
                assert(bulk_count <= ID_W'(ORDER_SLOTS));
            if ($past(m_valid && m_ready && event_kind == `RM_EV_REPLACE))
                assert(live == $past(live));
            if (import_open) begin
                assert(staged_count == $countones(staged_live));
                assert(staged_count <= staged_expected);
            end
            if ($past(m_valid && m_ready && event_kind == `RM_EV_IMPORT)) begin
                if ($past(tif) == `RM_IMPORT_COMMIT) begin
                    assert(live == $past(staged_live));
                    assert(live_count == $past(staged_count));
                    assert(!import_open);
                end else begin
                    assert(live == $past(live));
                end
            end
            for (integer i = 0; i < ORDER_SLOTS; i = i + 1) begin
                if (live[i]) begin
                    assert(total[i] != '0);
                    assert(visible[i] != '0 && visible[i] <= total[i]);
                    assert(peak[i] == '0 ? visible[i] == total[i] : visible[i] <= peak[i]);
                    assert(age[i] < live_count);
                    for (integer j = i+1; j < ORDER_SLOTS; j = j + 1) begin
                        if (live[j]) begin
                            assert(age[i] != age[j]);
                            if (side[i] != side[j])
                                assert(side[i] ? price[i] > price[j] : price[j] > price[i]);
                        end
                    end
                    if ($past(m_valid && m_ready && (event_kind == `RM_EV_REJECT ||
                        event_kind == `RM_EV_MASS_PREVIEW))) begin
                        assert(age[i] == $past(age[i]));
                        assert(price[i] == $past(price[i]));
                        assert(side[i] == $past(side[i]));
                        assert(owner[i] == $past(owner[i]));
                        assert(total[i] == $past(total[i]));
                        assert(visible[i] == $past(visible[i]));
                        assert(peak[i] == $past(peak[i]));
                    end
                    if ($past(m_valid && m_ready && event_kind == `RM_EV_REPLACE &&
                        slot == ADDR_W'(i))) begin
                        assert(owner[i] == $past(owner[i]));
                        assert(side[i] == $past(side[i]));
                        assert(total[i] == $past(event_total));
                        assert(price[i] == $past(event_price));
                        if ($past(event_refresh))
                            assert(age[i] == live_count-1'b1);
                        else begin
                            assert(age[i] == $past(age[i]));
                            assert(visible[i] <= $past(visible[i]));
                        end
                    end
                end
                if ($past(m_valid && m_ready && event_kind == `RM_EV_MASS_CANCEL &&
                    bulk_row && bulk_slot == ADDR_W'(i)))
                    assert(!live[i]);
            end
            cover(m_valid && m_ready && event_kind == `RM_EV_FILL && event_refresh);
            cover(m_valid && m_ready && event_kind == `RM_EV_STP_MAKER);
            cover(m_valid && m_ready && event_kind == `RM_EV_STP_TAKER);
            cover(m_valid && m_ready && event_kind == `RM_EV_REJECT && reason == `RM_ERR_FOK);
            cover(m_valid && m_ready && tif == `RM_FOK && event_kind == `RM_EV_FILL && event_last);
            cover(m_valid && m_ready && event_kind == `RM_EV_FILL &&
                  $past(m_valid && !m_ready) && event_refresh);
            cover(m_valid && m_ready && event_kind == `RM_EV_IMPORT && tif == `RM_IMPORT_COMMIT &&
                  staged_count > 1);
            cover(m_valid && !m_ready && event_kind == `RM_EV_SNAPSHOT && !event_last);
            cover(m_valid && m_ready && event_kind == `RM_EV_REPLACE && event_refresh);
            cover(m_valid && m_ready && event_kind == `RM_EV_REPLACE && !event_refresh &&
                  $past(m_valid && !m_ready));
            cover(m_valid && m_ready && event_kind == `RM_EV_MASS_CANCEL && event_last &&
                  event_count == 2);
            cover(m_valid && !m_ready && event_kind == `RM_EV_MASS_PREVIEW && !event_last);
        end
    end
