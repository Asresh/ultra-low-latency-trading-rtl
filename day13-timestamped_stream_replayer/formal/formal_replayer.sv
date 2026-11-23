`default_nettype none
`include "replay_codes.svh"

// Small finite image with arbitrary commands and independently stalled sinks.
module formal_replayer(input wire clk);
    localparam int DEPTH = 2, CW = $clog2(DEPTH+1);
    (* anyseq *) wire rst;
    (* anyseq *) wire cfg_valid, rsp_ready, m_ready;
    (* anyseq *) wire [2:0] cfg_op;
    (* anyseq *) wire [31:0] cfg_tick, cfg_gap;
    (* anyseq *) wire [63:0] cfg_data;
    (* anyseq *) wire [7:0] cfg_keep;
    (* anyseq *) wire cfg_last;
    (* anyseq *) wire [15:0] cfg_tag, cfg_repeats;
    wire cfg_ready, rsp_valid, m_valid, m_last, active, paused, done;
    wire [3:0] rsp_status;
    wire [CW-1:0] rsp_length;
    wire [63:0] m_data;
    wire [7:0] m_keep;
    wire [15:0] m_tag, m_pass;
    wire [47:0] m_due, m_offered, now_tick;
    wire [31:0] emitted_beats, emitted_packets, late_beats, completed_passes;

    timestamped_stream_replayer #(.DEPTH(DEPTH)) dut (.*);

    logic past_valid = 0;
    logic [CW-1:0] logical_length = 0;
    logic recorded_valid = 0;
    logic recorded_last;
    logic [15:0] recorded_tag, recorded_pass;
    logic seen_stall = 0;
    logic seen_second_pass = 0;
    logic seen_bad_keep = 0;
    logic seen_hold_through_pause = 0;

    wire request = cfg_valid && cfg_ready;
    wire transfer = m_valid && m_ready;

    // Match the append mask contract without inspecting the RTL's current input logic.
    function automatic logic allowed_keep(input logic [7:0] mask);
        case (mask)
            8'h01,8'h03,8'h07,8'h0f,8'h1f,8'h3f,8'h7f,8'hff: allowed_keep = 1;
            default: allowed_keep = 0;
        endcase
    endfunction

    always_ff @(posedge clk) begin
        past_valid <= 1;
        if (!past_valid)
            assume (rst);
        if (past_valid && !$past(rst) && $past(cfg_valid && !cfg_ready) && !rst)
            assume ($stable({cfg_valid,cfg_op,cfg_tick,cfg_data,cfg_keep,cfg_last,cfg_tag,cfg_repeats,cfg_gap}));

        if (past_valid) begin
            assert (!cfg_ready || (!rst && (!rsp_valid || rsp_ready)));
            assert (!paused || active);
            assert (!done || !active);
            assert (!m_valid || active);
            if (rsp_valid)
                assert (rsp_length <= DEPTH);
            if (m_valid) begin
                assert (m_pass != 0);
                assert (m_offered >= m_due);
                assert (m_offered <= now_tick);
                assert (allowed_keep(m_keep));
                assert (m_last || m_keep == 8'hff);
                for (integer lane = 0; lane < 8; lane++)
                    if (!m_keep[lane])
                        assert (m_data[8*lane+:8] == 0);
            end
            if ($past(rst)) begin
                assert (!active && !paused && !done && !m_valid && !rsp_valid);
                assert (now_tick == 0);
                assert (emitted_beats == 0 && late_beats == 0);
                assert (emitted_packets == 0 && completed_passes == 0);
            end
            if (!$past(rst)) begin
                if ($past(m_valid && !m_ready)) begin
                    assert (m_valid);
                    assert ($stable({m_data,m_keep,m_last,m_tag,m_due,m_offered,m_pass}));
                end
                if ($past(rsp_valid && !rsp_ready)) begin
                    assert (rsp_valid);
                    assert ($stable({rsp_status,rsp_length}));
                end
                if ($past(transfer)) begin
                    assert (emitted_beats == ((&$past(emitted_beats)) ? $past(emitted_beats)
                              : $past(emitted_beats)+1'b1));
                    if ($past(m_last) && !$past(&emitted_packets))
                        assert (emitted_packets == $past(emitted_packets)+1'b1);
                    else
                        assert (emitted_packets == $past(emitted_packets));
                    if ($past(m_offered > m_due) && !$past(&late_beats))
                        assert (late_beats == $past(late_beats)+1'b1);
                    else
                        assert (late_beats == $past(late_beats));
                end
                if ($past(active && paused && !done))
                    assert (now_tick == $past(now_tick));
                if ($past(request && (cfg_op == `RP_APPEND || cfg_op == `RP_CLEAR) && active)) begin
                    assert (rsp_valid && rsp_status == `RP_BUSY);
                    assert (rsp_length == logical_length);
                end
            end
        end

        if (rsp_valid && $past(request) && past_valid) begin
            assert (rsp_length <= DEPTH);
            if (rsp_status == `RP_OK && $past(cfg_op == `RP_APPEND))
                logical_length <= rsp_length;
            else if (rsp_status == `RP_OK && $past(cfg_op == `RP_CLEAR))
                logical_length <= 0;
        end

        if (rst || !active) begin
            recorded_valid <= 0;
            seen_stall <= 0;
            seen_second_pass <= 0;
            seen_hold_through_pause <= 0;
            if (rst) begin
                logical_length <= 0;
                seen_bad_keep <= 0;
            end
        end else begin
            if (transfer) begin
                if (recorded_valid && !recorded_last)
                    assert ({m_pass,m_tag} == {recorded_pass,recorded_tag});
                recorded_valid <= 1;
                recorded_pass <= m_pass;
                recorded_tag <= m_tag;
                recorded_last <= m_last;
            end
            if (m_valid && !m_ready)
                seen_stall <= 1;
            if (m_valid && !m_ready && paused)
                seen_hold_through_pause <= 1;
            if (m_valid && m_pass == 2)
                seen_second_pass <= 1;
        end
        if (rsp_valid && rsp_status == `RP_BAD_KEEP)
            seen_bad_keep <= 1;

        cover (past_valid && active && m_valid && seen_bad_keep);
        cover (past_valid && active && transfer && seen_stall && seen_second_pass);
        cover (past_valid && active && transfer && seen_hold_through_pause);
        cover (past_valid && done && completed_passes == 2);
    end
endmodule

`default_nettype wire
