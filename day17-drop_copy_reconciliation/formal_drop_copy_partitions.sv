`timescale 1ns/1ps
`default_nettype none
`include "reconcile_defs.svh"

module formal_drop_copy_partitions (
    input wire clk,
    input wire rst,
    input wire s_primary_valid, s_copy_valid, s_epoch_valid, m_ready,
    input wire [1:0] s_primary_namespace, s_copy_namespace, s_epoch_namespace,
    input wire [1:0] primary_epoch, copy_epoch, requested_epoch,
    input wire [2:0] primary_execution, copy_execution,
    input wire [3:0] primary_value, copy_value
);
    localparam integer NAMESPACES = 2, COUNT_W = 2;
    wire [15:0] s_new_epoch = {14'd0,requested_epoch};
    wire [`DC_KEY_W-1:0] s_primary_key = {14'd0,primary_epoch,24'd0,29'd0,primary_execution,2'd0};
    wire [`DC_KEY_W-1:0] s_copy_key = {14'd0,copy_epoch,24'd0,29'd0,copy_execution,2'd0};
    wire [`DC_PAYLOAD_W-1:0] s_primary_payload = {32'd1,24'd1,28'd0,primary_value,1'b0};
    wire [`DC_PAYLOAD_W-1:0] s_copy_payload = {32'd1,24'd1,28'd0,copy_value,1'b0};

    wire s_primary_ready, s_copy_ready, s_epoch_ready, m_valid;
    wire [1:0] m_namespace, m_source;
    wire [3:0] m_status, m_diff;
    wire [`DC_KEY_W-1:0] m_key;
    wire [`DC_PAYLOAD_W-1:0] m_expected, m_observed;
    wire [31:0] m_age, bad_namespace_count;
    wire [COUNT_W-1:0] m_flushed;
    wire [NAMESPACES*16-1:0] active_epochs;
    wire [NAMESPACES*COUNT_W-1:0] pending_counts;
    wire [NAMESPACES*320-1:0] namespace_metrics;
    wire [NAMESPACES-1:0] namespace_incidents;
    wire epoch_fire = s_epoch_valid && s_epoch_ready;
    logic sampled = 1'b0;

    drop_copy_partitions #(
        .NAMESPACES(NAMESPACES), .SETS(1), .WAIT_CYCLES(3), .HISTORY_CYCLES(5)
    ) dut (.*);

    always @(posedge clk) begin
        sampled <= 1'b1;
        if (!rst) begin
            assert (!(s_primary_valid && s_primary_ready && s_copy_valid && s_copy_ready &&
                      s_primary_namespace == s_copy_namespace));
            if (m_valid && m_status == `DC_BAD_NAMESPACE)
                assert (m_namespace >= 2);
            if (sampled && !$past(rst) && $past(m_valid && !m_ready))
                assert (m_valid && {m_namespace,m_status,m_source,m_key,m_expected,m_observed,
                        m_diff,m_age,m_flushed} == $past({m_namespace,m_status,m_source,m_key,
                        m_expected,m_observed,m_diff,m_age,m_flushed}));
        end
        if (sampled && $past(rst)) begin
            assert (active_epochs == 0 && pending_counts == 0);
            assert (namespace_metrics == 0 && namespace_incidents == 0 && bad_namespace_count == 0);
        end
    end

    for (genvar namespace = 0; namespace < NAMESPACES; namespace++) begin : g_domain
        wire [15:0] active = active_epochs[16*namespace +: 16];
        wire targeted = epoch_fire && s_epoch_namespace == 2'(namespace) && s_new_epoch > active;
        always @(posedge clk) begin
            if (sampled && !rst && !$past(rst)) begin
                if ($past(targeted)) begin
                    assert (active == $past(s_new_epoch));
                    assert (!namespace_incidents[namespace]);
                end else begin
                    assert (active == $past(active));
                    if ($past(namespace_incidents[namespace]))
                        assert (namespace_incidents[namespace]);
                end
            end
        end
    end
endmodule

`default_nettype wire
