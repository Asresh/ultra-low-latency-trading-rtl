`default_nettype none

module formal_banked_replayer;
    (* gclk *) logic clk;
    (* anyseq *) logic rst, cfg_valid, cfg_bank, cfg_last, m_ready, rsp_ready;
    (* anyseq *) logic [2:0] cfg_op;
    (* anyseq *) logic [2:0] cfg_run_id;
    (* anyseq *) logic [2:0] small_tick, small_repeats, small_gap;
    (* anyseq *) logic [4:0] small_release;
    (* anyseq *) logic [7:0] cfg_keep, small_data;
    (* anyseq *) logic [3:0] small_tag;
    wire [63:0] cfg_release_tick = {59'd0,small_release};
    wire [31:0] cfg_tick = {29'd0,small_tick};
    wire [63:0] cfg_data = {56'd0,small_data};
    wire [15:0] cfg_tag = {12'd0,small_tag};
    wire [15:0] cfg_repeats = {13'd0,small_repeats};
    wire [31:0] cfg_gap = {29'd0,small_gap};

    wire cfg_ready, rsp_valid, m_valid, m_last, active, paused, m_bank, run_started, done;
    wire [3:0] rsp_status;
    wire [2:0] rsp_op, rsp_run_id, m_run_id, done_run_id;
    wire rsp_bank, rsp_image_open, done_bank, done_stopped, done_truncated;
    wire [1:0] rsp_bank_state, rsp_length;
    wire [63:0] m_data, wall_tick, run_start_tick, done_tick;
    wire [7:0] m_keep;
    wire [15:0] m_tag, m_pass;
    wire [47:0] m_due, m_offered, now_tick;
    wire [31:0] done_beats, done_packets, done_late, done_passes;
    wire [31:0] started_runs, retired_runs, cancelled_runs, blocked_release_cycles;

    banked_stream_replayer #(.DEPTH(2), .RUN_W(3)) dut (.*);
    logic initialized = 0;
    always @(posedge clk) begin
        if (!initialized)
            assume(rst);
        initialized <= 1;
        if (initialized && !$past(rst) && !rst) begin
            if ($past(cfg_valid && !cfg_ready)) begin
                assume(cfg_valid);
                assume($stable({cfg_op,cfg_bank,cfg_run_id,cfg_tick,cfg_data,cfg_keep,
                                cfg_last,cfg_tag,cfg_repeats,cfg_gap,cfg_release_tick}));
            end
            if ($past(m_valid && !m_ready)) begin
                assert(m_valid);
                assert($stable({m_data,m_keep,m_last,m_tag,m_due,m_offered,m_pass,m_bank,m_run_id}));
            end
            if ($past(rsp_valid && !rsp_ready)) begin
                assert(rsp_valid);
                assert($stable({rsp_status,rsp_op,rsp_bank,rsp_run_id,rsp_bank_state,
                                rsp_length,rsp_image_open}));
            end
            if (m_valid) begin
                assert(active && m_run_id != 0 && m_offered >= m_due && m_pass != 0);
                assert(m_keep && (m_keep & (m_keep + 1'b1)) == 0);
            end
            if (active && $past(active) && !run_started)
                assert($stable({m_bank,m_run_id}));
            if (run_started) begin
                assert(active && m_run_id != 0);
                assert(run_start_tick == $past(wall_tick));
                assert(!$past(active) || done);
            end
            if (done) begin
                assert(done_run_id != 0);
                assert(!done_truncated || done_stopped);
                assert(done_tick == $past(wall_tick));
                assert(!run_started || (done_bank != m_bank && done_run_id < m_run_id));
                assert(done_packets <= done_beats && done_late <= done_beats);
            end
            assert(retired_runs <= started_runs);
            cover(done && run_started);
            cover(done && done_truncated);
            cover(rsp_valid && rsp_status == 11);
            cover(cfg_valid && cfg_ready && cfg_op == 0 && active && cfg_bank != m_bank);
        end
    end
endmodule

`default_nettype wire
