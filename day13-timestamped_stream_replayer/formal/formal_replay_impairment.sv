`default_nettype none

module formal_replay_impairment;
    localparam integer RUN_W = 2, REV_W = 2, RULES = 1, KEY_W = 1;
    localparam integer RULE_W = 198+RUN_W;
    localparam integer AUDIT_W = 280+RUN_W+REV_W+KEY_W;
    (* gclk *) logic clk;
    (* anyseq *) logic rst, cfg_valid, rsp_ready, s_valid, m_ready, audit_ready;
    (* anyseq *) logic s_first, s_last, s_bank, abort_valid, abort_bank;
    (* anyseq *) logic [RUN_W-1:0] s_run, abort_run;
    (* anyseq *) logic [REV_W-1:0] cfg_expected_revision;
    (* anyseq *) logic [1:0] cfg_op, inspect_action, inspect_delay;
    (* anyseq *) logic [7:0] s_keep, low_data;
    (* anyseq *) logic low_tag;
    wire [63:0] s_data = 64'(low_data);
    wire [15:0] s_tag = 16'(low_tag), s_pass = 16'd1;
    wire [47:0] s_due = '0, s_offered = '0;
    wire [KEY_W-1:0] cfg_slot = 0;
    wire cfg_read_active = 1;
    wire [31:0] cfg_seed = '0;
    wire [RULE_W-1:0] cfg_rule = {
        1'b1,16'd0,16'd0,1'b1,RUN_W'(0),1'b1,1'b0,32'd0,32'hffffffff,inspect_action,
        32'(inspect_delay),8'd0,32'd1,16'd0,8'h10
    };
    logic [63:0] wall_tick;

    wire cfg_ready, rsp_valid, s_ready, m_valid, m_first, m_last, m_copy, m_bank, audit_valid;
    wire [1:0] rsp_op, m_action;
    wire [2:0] rsp_status;
    wire [REV_W-1:0] rsp_revision, m_revision;
    wire [KEY_W-1:0] rsp_slot;
    wire [31:0] rsp_seed, m_ordinal;
    wire [RULE_W-1:0] rsp_rule;
    wire [AUDIT_W-1:0] audit_data;
    wire [63:0] audit_sequence, audit_cursor, m_data, m_eligible;
    wire [7:0] m_keep;
    wire [15:0] m_tag, m_pass;
    wire [47:0] m_due, m_offered;
    wire [RUN_W-1:0] m_run;
    wire occupancy, peak_occupancy;
    wire [31:0] frames, drops, duplicates, corruptions, flip_misses;
    wire [31:0] malformed, oversizes, aborted, emitted, lost_audits, sink_stall_cycles;

    // The SBY script elaborates this instance's compact geometry.
    replay_impairment dut (.*);

    logic past_valid = 0;
    always @(posedge clk) begin
        past_valid <= 1;
        if (!past_valid)
            assume(rst);
        if (rst)
            wall_tick <= 0;
        else if (!(&wall_tick))
            wall_tick <= wall_tick + 1'b1;
        if (rst)
            assert(!m_valid && !rsp_valid && !s_ready && !audit_valid && !cfg_ready);
        if (past_valid && !$past(rst) && !rst) begin
            if ($past(s_valid && !s_ready)) begin
                assume(s_valid);
                assume($stable({s_data,s_keep,s_first,s_last,s_tag,s_bank,s_run}));
            end
            if ($past(m_valid && !m_ready)) begin
                assert(m_valid);
                assert($stable({m_data,m_keep,m_first,m_last,m_tag,m_pass,m_due,m_offered,m_bank,
                                m_run,m_copy,m_revision,m_ordinal,m_action,m_eligible}));
            end
            if ($past(audit_valid && !audit_ready)) begin
                assert(audit_valid);
                assert($stable({audit_sequence,audit_data}));
            end
            if ($past(rsp_valid && !rsp_ready)) begin
                assert(rsp_valid);
                assert($stable({rsp_op,rsp_status,rsp_revision,rsp_slot,rsp_seed,rsp_rule}));
            end
            if (m_valid) begin
                assert(occupancy && wall_tick >= m_eligible);
                assert(m_action != 1 && (!m_copy || m_action == 2));
                assert(m_keep && ((m_keep & (m_keep+1'b1)) == 0));
                assert(m_last || m_keep == 8'hff);
            end
            if (audit_valid)
                assert(audit_sequence < audit_cursor);
            assert(peak_occupancy >= occupancy);
            cover(m_valid && m_copy && m_last && m_ready);
            cover(m_valid && m_action == 3 && m_ready);
            cover(aborted != 0);
            cover(occupancy && drops != 0);
        end
    end
endmodule

`default_nettype wire
