`default_nettype none

module formal_mold_session(input wire clk);
    localparam integer SLOTS = 3, KEY_W = 2, SLOT_W = 2, EPOCH_W = 2, WAIT_W = 3;

    logic past_valid = 1'b0;
    wire rst = !past_valid;
    (* anyseq *) wire cfg_valid, cfg_enable, cfg_force, ack_ready;
    (* anyseq *) wire [KEY_W-1:0] cfg_slot;
    (* anyseq *) wire [EPOCH_W-1:0] cfg_expected_epoch;
    (* anyseq *) wire [79:0] cfg_session;
    (* anyseq *) wire [63:0] cfg_sequence;
    (* anyseq *) wire [WAIT_W-1:0] cfg_request_spacing;
    (* anyseq *) wire [SLOTS-1:0] leased, output_pending, install_ready;
    (* anyseq *) wire [1:0] cut_lanes;
    wire cfg_ready, ack_valid, ack_active, cut_valid;
    wire [1:0] ack_status, ack_cut_lanes;
    wire [KEY_W-1:0] ack_slot;
    wire [SLOT_W-1:0] cut_slot;
    wire [EPOCH_W-1:0] ack_epoch;
    wire [SLOTS-1:0] active, fenced, install;
    wire [SLOTS*EPOCH_W-1:0] epochs;
    wire [SLOTS*WAIT_W-1:0] request_spacing;
    wire [79:0] install_session;
    wire [63:0] install_sequence;

    (* anyseq *) wire [SLOTS-1:0] s_valid;
    (* anyseq *) wire [SLOTS*16-1:0] s_data;
    (* anyseq *) wire m_ready;
    wire [SLOTS-1:0] s_ready;
    wire m_valid;
    wire [SLOT_W-1:0] m_slot;
    wire [15:0] m_data;
    wire buffered = |s_ready;

    mold_session_config #(.SLOTS(SLOTS), .EPOCH_W(EPOCH_W), .WAIT_W(WAIT_W)) cfg (.*);
    mold_session_mux #(.SLOTS(SLOTS), .WIDTH(16)) mux (.*);

    function automatic logic one_or_zero(input logic [SLOTS-1:0] mask);
        one_or_zero = (mask & (mask - 1'b1)) == 0;
    endfunction

    logic [KEY_W-1:0] pending_slot;
    logic [79:0] pending_session;
    logic [63:0] pending_sequence;
    logic [WAIT_W-1:0] pending_spacing;
    logic [1:0] pending_cuts;
    logic pending_enable;
    wire accepted = cfg_ready && cfg_valid;
    wire good_slot = int'(cfg_slot) < SLOTS;
    wire [EPOCH_W-1:0] previous = good_slot ? epochs[cfg_slot*EPOCH_W+:EPOCH_W] : '0;
    wire starts = accepted && good_slot && previous == cfg_expected_epoch && previous != '1;

    always @(posedge clk) begin
        past_valid <= 1'b1;
        if (starts) begin
            pending_slot <= cfg_slot;
            pending_session <= cfg_session;
            pending_sequence <= cfg_sequence;
            pending_spacing <= cfg_request_spacing;
            pending_enable <= cfg_enable;
            pending_cuts <= cfg_force ? cut_lanes : '0;
        end
        if (past_valid) begin
            assert(one_or_zero(s_ready));
            assert(one_or_zero(install));
            assert((s_ready & ~s_valid) == '0);
            assert((install & leased) == '0);
            assert((install & output_pending) == '0);
            assert((install & ~install_ready) == '0);
            assert((install & ~fenced) == '0);
            if (starts) begin
                assert(fenced[cfg_slot]);
                assert(cut_valid == cfg_force);
                assert(cut_slot == cfg_slot);
            end else begin
                assert(!cut_valid);
            end
            if (|install) begin
                assert(install[pending_slot]);
                assert(install_session == pending_session);
                assert(install_sequence == pending_sequence);
                assert(!cfg_ready);
            end
            if ($past(!rst)) begin
                if ($past(m_valid && !m_ready)) begin
                    assert(m_valid);
                    assert({m_slot,m_data} == $past({m_slot,m_data}));
                end
                if ($past(!m_valid || m_ready))
                    assert(m_valid == $past(buffered));
                if ($past(ack_valid && !ack_ready)) begin
                    assert(ack_ready || !cfg_ready);
                    assert(ack_valid);
                    assert({ack_status,ack_slot,ack_epoch,ack_active,ack_cut_lanes} ==
                           $past({ack_status,ack_slot,ack_epoch,ack_active,ack_cut_lanes}));
                end
                if ($past(accepted && !starts)) begin
                    assert(ack_valid && ack_slot == $past(cfg_slot));
                    if ($past(!good_slot))
                        assert(ack_status == 1);
                    else if ($past(previous != cfg_expected_epoch))
                        assert(ack_status == 2);
                    else
                        assert(ack_status == 3);
                end
                if ($past(|install)) begin
                    assert(ack_valid && ack_status == 0);
                    assert(ack_slot == $past(pending_slot));
                    assert(ack_active == $past(pending_enable));
                    assert(ack_cut_lanes == $past(pending_cuts));
                end
            end
            cover(ack_valid && ack_status == 0 && ack_cut_lanes == 2'b11);
            cover(ack_valid && ack_status == 3 && ack_epoch == '1);
            cover(ack_valid && ack_status == 2 && ack_epoch != 0);
            cover(m_valid && m_ready && buffered && s_ready != (3'b001 << m_slot));
        end
    end

    for (genvar slot = 0; slot < SLOTS; slot++) begin : per_slot
        localparam integer BASE = slot*EPOCH_W;
        localparam integer PACE = slot*WAIT_W;
        wire [EPOCH_W-1:0] current_epoch = epochs[BASE+:EPOCH_W];
        logic [SLOT_W:0] bypasses;
        always @(posedge clk) begin
            if (rst || !s_valid[slot] || s_ready[slot])
                bypasses <= '0;
            else if (buffered)
                bypasses <= bypasses + 1'b1;
            if (past_valid && $past(!rst)) begin
                assert(bypasses < SLOTS);
                if ($past(s_ready[slot])) begin
                    assert(m_valid && m_slot == slot);
                    assert(m_data == $past(s_data[slot*16+:16]));
                end
                if ($past(install[slot])) begin
                    assert(current_epoch == $past(current_epoch) + 1'b1);
                    assert(current_epoch != 0);
                    assert(request_spacing[PACE+:WAIT_W] == $past(pending_spacing));
                    assert(active[slot] == $past(pending_enable));
                    assert(ack_epoch == current_epoch);
                end else begin
                    assert(current_epoch == $past(current_epoch));
                    assert(active[slot] == $past(active[slot]));
                    assert(request_spacing[PACE+:WAIT_W] == $past(request_spacing[PACE+:WAIT_W]));
                end
            end
        end
    end
endmodule

`default_nettype wire
