`timescale 1ns/1ps
`default_nettype none
`include "reconcile_defs.svh"

module formal_drop_copy(
    input wire clk,
    input wire rst,
    input wire s_primary_valid, s_copy_valid, s_epoch_valid, m_ready,
    input wire [`DC_KEY_W-1:0] s_primary_key, s_copy_key,
    input wire [`DC_PAYLOAD_W-1:0] s_primary_payload, s_copy_payload,
    input wire [15:0] s_new_epoch
);
    wire s_primary_ready, s_copy_ready, s_epoch_ready, m_valid;
    wire [3:0] m_status, m_diff;
    wire [1:0] m_source;
    wire [`DC_KEY_W-1:0] m_key;
    wire [`DC_PAYLOAD_W-1:0] m_expected, m_observed;
    wire [31:0] m_age, tick;
    wire [1:0] m_flushed, pending_count;
    wire [15:0] active_epoch;
    wire [31:0] observed_count, matched_count, conflict_count, duplicate_count;
    wire [31:0] missing_count, late_count, full_count, rejected_count, flushed_count;
    wire [`DC_KEY_W+2*`DC_PAYLOAD_W+43:0] report =
        {m_status,m_source,m_key,m_expected,m_observed,m_diff,m_age,m_flushed};

    logic past_valid = 0;
    wire data_fire = (s_primary_valid && s_primary_ready) || (s_copy_valid && s_copy_ready);
    wire cfg_fire = s_epoch_valid && s_epoch_ready;

    drop_copy_reconciliation #(
        .SETS(1), .WAIT_CYCLES(3), .HISTORY_CYCLES(5), .RESET_TICK(32'hffff_fffc)
    ) dut (.*);

    always @(posedge clk) begin
        past_valid <= 1;
        if (!past_valid) assume (rst);
        if (past_valid && !rst) begin
            assert (pending_count <= 2);
            assert (!(s_primary_ready && s_copy_ready));
            assert (!(cfg_fire && data_fire));
            if (!$past(rst)) begin
                assert (tick == $past(tick) + 1);
                assert (active_epoch >= $past(active_epoch));
                if ($past(m_valid && !m_ready)) begin
                    assert (m_valid);
                    assert (report == $past(report));
                    assert (pending_count == $past(pending_count));
                    if (!m_ready) assert (!data_fire && !cfg_fire);
                end
                if ($past(cfg_fire && s_new_epoch > active_epoch)) begin
                    assert (active_epoch == $past(s_new_epoch));
                    assert (m_status == `DC_RESTART && m_valid && m_source == 3);
                    assert (m_flushed == $past(pending_count));
                    assert (pending_count == 0);
                end
                if ($past(s_primary_valid && s_primary_ready)) begin
                    assert (m_valid && m_source == 0);
                    assert (m_key == $past(s_primary_key));
                    assert (m_observed == $past(s_primary_payload));
                end
                if ($past(s_copy_valid && s_copy_ready)) begin
                    assert (m_valid && m_source == 1);
                    assert (m_key == $past(s_copy_key));
                    assert (m_observed == $past(s_copy_payload));
                end
            end
            cover (m_valid && m_status == `DC_MATCH && tick == 1 &&
                   $past(data_fire) && m_age == 2);
            cover (m_valid && m_status == `DC_RESTART && m_flushed == 2);
            cover (m_valid && m_status == `DC_LATE);
        end
    end
endmodule

`default_nettype wire
