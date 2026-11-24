`timescale 1ns/1ps
`default_nettype none
`include "fanout_status.svh"

// Bounded management and ownership checks at a small geometry. All endpoint
// controls, payloads, masks, and resetting edges after initialization vary freely.
module formal_multicast_packet_fanout (
    input wire clk,
    input wire rst,
    input wire cfg_valid,
    input wire cfg_group,
    input wire [1:0] cfg_destinations,
    input wire cfg_drop_on_congestion,
    input wire s_valid,
    input wire [7:0] s_data,
    input wire s_keep,
    input wire s_last,
    input wire s_group,
    input wire [3:0] s_tag,
    input wire [1:0] m_ready,
    input wire report_ready
);
    wire cfg_ready, s_ready;
    wire [1:0] m_valid, m_last, m_keep;
    wire [15:0] m_data;
    wire [7:0] m_tag;
    wire report_valid;
    wire [2:0] report_status;
    wire [3:0] report_tag;
    wire report_group;
    wire [1:0] report_destinations;
    wire [15:0] report_bytes;
    wire [31:0] forwarded_packets, filtered_packets, congested_packets, invalid_packets;
    wire [63:0] delivered_packets;
    wire [1:0] port_occupancy, resident_slots;

    multicast_packet_fanout #(
        .DATA_BYTES(1), .PORTS(2), .GROUPS(1), .SLOTS(2),
        .PORT_DEPTH(1), .MAX_BEATS(2), .TAG_W(4)
    ) dut (.*);

    reg past_valid = 1'b0;
    wire finish_ingress = s_valid && s_ready && s_last;
    wire [24:0] report_fields = {report_status, report_tag, report_group,
                                  report_destinations, report_bytes};
    wire [31:0] classifications = forwarded_packets + filtered_packets +
                                  congested_packets + invalid_packets;

    always @(posedge clk) begin
        past_valid <= 1'b1;
        if (past_valid) begin
            assert(resident_slots <= 2);
            assert(resident_slots <= {1'b0, port_occupancy[0]} + {1'b0, port_occupancy[1]} + 2'd1);
            if ($past(rst)) begin
                assert(port_occupancy == 0 && resident_slots == 0);
                assert(classifications == 0 && delivered_packets == 0);
                assert(!report_valid);
            end else if (!rst) begin
                assert(classifications == $past(classifications) + 32'($past(finish_ingress)));
                if ($past(finish_ingress)) begin
                    assert(report_valid);
                    assert((forwarded_packets != $past(forwarded_packets)) ==
                           (report_status == `FANOUT_FORWARDED));
                end
                if ($past(report_valid && !report_ready)) begin
                    assert(report_valid);
                    assert(report_fields == $past(report_fields));
                end
            end
        end
    end

    for (genvar port_id = 0; port_id < 2; port_id++) begin : g_formal_port
        wire [31:0] delivered = delivered_packets[port_id*32+:32];
        wire [13:0] payload = {
            m_last[port_id], m_tag[port_id*4+:4], m_keep[port_id], m_data[port_id*8+:8]
        };
        wire retire = m_valid[port_id] && m_ready[port_id] && m_last[port_id];
        always @(posedge clk) begin
            if (past_valid) begin
                assert(port_occupancy[port_id] <= resident_slots);
                assert(delivered <= forwarded_packets);
                assert(m_valid[port_id] == (!rst && port_occupancy[port_id]));
                if (!rst && !$past(rst)) begin
                    if ($past(m_valid[port_id] && !m_ready[port_id])) begin
                        assert(m_valid[port_id]);
                        assert(payload == $past(payload));
                    end
                    assert(delivered == $past(delivered) + 32'($past(retire)));
                    // A forwarded packet produces one reference on each reported
                    // destination and every last beat frees exactly one reference.
                    assert(2'(port_occupancy[port_id]) + 2'($past(retire)) ==
                        2'($past(port_occupancy[port_id])) +
                        2'((forwarded_packets != $past(forwarded_packets)) &&
                           report_destinations[port_id]));
                end
            end
        end
    end
endmodule

`default_nettype wire
