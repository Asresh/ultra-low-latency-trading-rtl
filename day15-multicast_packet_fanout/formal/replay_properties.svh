    logic sampled = 0;
    always @(posedge clk) begin
        sampled <= 1;
        if (!sampled)
            assume(rst);
        if (rst)
            assert(!cfg_ready && !s_ready && !report_valid && !cfg_report_valid && m_valid == 0);
        if (sampled && !rst) begin
            assert(retained_packets == $countones(retained_slots));
            assert(retained_packets <= RETAIN && retained_packets <= next_sequence);
            assert(resident_slots == $countones(occupied_slots));
            if (!assembling)
                assert(free_found);
            if (assembling)
                assert(!retained_slots[assembly_slot]);
            if (accept_beat && !assembling)
                assert(!occupied_slots[input_slot]);
            if (publish)
                assert(!retained_slots[input_slot] && !exhausted);
            if (exhausted)
                assert(!accept_beat);
            if ($past(rst)) begin
                assert(next_sequence == $past(reset_sequence));
                assert(epoch == $past(reset_epoch));
                assert(retained_packets == 0 && resident_slots == 0);
            end else begin
                assert(epoch == $past(epoch));
                assert(next_sequence == $past(next_sequence) + SEQUENCE_W'($past(publish)));
                if ($past(report_valid && !report_ready)) begin
                    assert(report_valid);
                    assert({report_status,report_tag,report_sequence} ==
                           $past({report_status,report_tag,report_sequence}));
                end
                if ($past(cfg_report_valid && !cfg_report_ready)) begin
                    assert(cfg_report_valid);
                    assert({cfg_report_status,cfg_report_op,cfg_report_port,cfg_report_tag,
                            cfg_report_enabled,cfg_report_cursor} ==
                           $past({cfg_report_status,cfg_report_op,cfg_report_port,cfg_report_tag,
                                  cfg_report_enabled,cfg_report_cursor}));
                end
            end
            cover(cfg_report_valid && cfg_report_ready && cfg_report_status == BUSY);
            cover(exhausted && retained_packets != 0);
            cover(report_valid && report_ready && report_status == `FANOUT_BAD_TAG);
        end
    end

    for (genvar p = 0; p < PORTS; p++) begin : properties_port
        wire [3+2*SEQUENCE_W+DATA_W+DATA_BYTES+TAG_W-1:0] packet_pins = {
            m_gap[p],m_first[p],m_last[p],m_sequence[p*SEQUENCE_W+:SEQUENCE_W],
            m_resume[p*SEQUENCE_W+:SEQUENCE_W],m_data[p*DATA_W+:DATA_W],
            m_keep[p*DATA_BYTES+:DATA_BYTES],m_tag[p*TAG_W+:TAG_W]
        };
        always @(posedge clk) if (sampled && !rst) begin
            if (mode[p] == DATA) begin
                assert(desired[p] < next_sequence);
                assert(occupied_slots[pinned[p]]);
                assert(cursor[p] <= last_beat[pinned[p]]);
                assert(!(accept_beat && input_slot == pinned[p]));
            end
            if (mode[p] == GAP)
                assert(desired[p] < resume[p] && resume[p] <= earliest_sequence);
            if ($past(!rst && m_valid[p] && !m_ready[p])) begin
                assert(m_valid[p] && packet_pins == $past(packet_pins));
            end
            if ($past(!rst && m_gap[p] && m_ready[p]))
                assert(desired[p] == $past(resume[p]));
            if ($past(!rst && m_valid[p] && !m_gap[p] && m_last[p] && m_ready[p]))
                assert(desired[p] == $past(desired[p])+1'b1);
            cover(mode[p] == DATA && !retained_slots[pinned[p]] && !m_ready[p] && publish);
            cover(mode[p] == GAP && resume[p] < earliest_sequence && !m_ready[p]);
        end
    end
