    logic formal_started = 0;
    wire [11+SESSION_W+GROUP_W+TAG_W+DATA_W+5*TOKEN_W+TTL_W+COUNT_W-1:0] reply = {
        m_admin,m_op,m_status,m_detail,m_session,m_group,m_tag,m_data,m_cost,m_ttl,
        m_deficit,m_depth,m_balances
    };
    always @(posedge clk) begin
        formal_started <= 1;
        if (!formal_started)
            assume(rst);
        if (rst)
            assert(!m_valid && !s_ready && !a_ready);
        if (formal_started && !rst && $past(!rst)) begin
            if ($past(m_valid && !m_ready)) begin
                assert(m_valid);
                assert(reply == $past(reply));
            end
            assert(cursor < SESSIONS);
            assert(!pop || (!manage && queued));
            if (admitted) begin
                assert(head_ttl != 0 && debit != 0);
                assert(port_live && group_live[chosen_group] && session_live[cursor]);
                assert(debit <= credited_deficit && debit <= session_tokens[cursor]);
                assert(needed_port <= {1'b0,port_tokens});
                assert(needed_group <= {1'b0,group_tokens[chosen_group]});
            end else begin
                assert(debit == 0);
            end
            if ($past(pop)) begin
                assert(m_valid && !m_admin);
                assert(m_status == $past(scheduled_status));
                if (m_status == EXPIRED)
                    assert(m_ttl == 0);
            end
            if ($past(s_rejected))
                assert(!$past(enqueue));
            cover(pop && enqueue && input_index == cursor && count[cursor] == DEPTH);
            cover(m_valid && !m_ready && !m_admin && m_status == PASS);
            cover(m_valid && m_ready && m_status == EXPIRED);
            cover(m_valid && m_ready && m_status == IMPOSSIBLE);
            cover(m_valid && m_ready && m_status == BUSY);
        end
    end
    for (genvar key = 0; key < SESSIONS; key++) begin : queue_invariants
        wire added = enqueue && input_index == SESSION_IDX_W'(key);
        wire removed = pop && cursor == SESSION_IDX_W'(key);
        always @(posedge clk) if (formal_started && !rst && $past(!rst)) begin
            assert(count[key] == $countones(live[key]));
            assert(count[key] <= COUNT_W'(DEPTH));
            if (count[key] != 0)
                assert(live[key][head[key]]);
            if ($past(added && !removed))
                assert(count[key] == $past(count[key])+1'b1);
            else if ($past(removed && !added))
                assert(count[key] == $past(count[key])-1'b1);
            else
                assert(count[key] == $past(count[key]));
        end
        for (genvar entry = 0; entry < DEPTH; entry++) begin : deadline
            wire install = added && tail[key] == PTR_W'(entry);
            always @(posedge clk) if (formal_started && !rst && $past(!rst)) begin
                if ($past(install)) begin
                    assert(live[key][entry]);
                    assert(ttl[key][entry] == $past(s_ttl));
                    assert(cost[key][entry] == $past(s_cost));
                end else if ($past(live[key][entry])) begin
                    if ($past(ttl[key][entry]) != 0)
                        assert(ttl[key][entry] == $past(ttl[key][entry])-1'b1);
                    else
                        assert(ttl[key][entry] == 0);
                end
            end
        end
    end
