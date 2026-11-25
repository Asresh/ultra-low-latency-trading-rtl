    logic started = 0;
    wire [4+2*BANKS+TAG_W+BANKS*3+BANKS*CLOCK_W-1:0] control_reply = {
        ctl_report_op,ctl_report_status,ctl_report_mask,ctl_report_tag,ctl_report_detail,
        ctl_report_armed,ctl_report_cutoffs
    };

    always @(posedge clk) begin
        started <= 1;
        if (!started)
            assume(rst);
        if (rst)
            assert(!s_ready && !ctl_ready && !ctl_report_valid && !m_valid);
        if (started && !rst) begin
            assert((schedule_due & ~schedule_armed) == 0);
            assert((schedule_queued & ~schedule_due) == 0);
            if ($past(rst)) begin
                assert(clock_tick == 0 && schedule_armed == 0);
                assert(!m_valid && !ctl_report_valid);
            end else begin
                assert(clock_tick == ($past(&clock_tick) ? $past(clock_tick) : $past(clock_tick)+1'b1));
                if ($past(manage && ctl_op == `AB_ARM))
                    assert((schedule_armed & $past(ctl_mask)) == $past(ctl_mask));
                if ($past(manage && ctl_op == `AB_CANCEL))
                    assert((schedule_armed & $past(ctl_mask)) == 0);
                if ($past(m_valid && !m_ready))
                    assert(m_valid && merged == $past(merged));
                if ($past(ctl_report_valid && !ctl_report_ready))
                    assert(ctl_report_valid && control_reply == $past(control_reply));
            end
            cover(schedule_due == {BANKS{1'b1}});
            cover(ctl_report_valid && ctl_report_status == `AB_ADMIN_ROLLBACK &&
                  ctl_report_mask == {BANKS{1'b1}} && ctl_report_detail != 0);
            cover(m_valid && m_scheduled && m_result[RESULT_W-9] && m_ready);
            cover(m_valid && m_wrapper_status == `AB_CUTOFF && m_ready);
        end
    end
