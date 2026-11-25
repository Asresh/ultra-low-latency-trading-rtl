`timescale 1ns/1ps
`default_nettype none

// The external demultiplexer leases a slot only on a first accepted beat.
// A forced epoch cut changes the lane to a discard lease until its last beat.
module mold_session_ingress #(
    parameter integer SLOTS = 3,
    localparam integer SLOT_W = SLOTS > 1 ? $clog2(SLOTS) : 1,
    localparam integer KEY_W = $clog2(SLOTS + 1)
) (
    input  wire clk,
    input  wire rst,
    input  wire [SLOTS-1:0] active,
    input  wire [SLOTS-1:0] fenced,
    input  wire cut_valid,
    input  wire [SLOT_W-1:0] cut_slot,
    output wire cut_lane,
    input  wire s_valid,
    output logic s_ready,
    input  wire s_first,
    input  wire s_last,
    input  wire [KEY_W-1:0] s_slot,
    output logic [SLOTS-1:0] slot_valid,
    input  wire [SLOTS-1:0] slot_ready,
    output logic [SLOTS-1:0] slot_leased,
    output wire discarded_packet,
    output logic [SLOTS-1:0] routed_packet
);
    logic attached, lease_routed;
    logic [SLOT_W-1:0] lease_slot;
    logic [SLOT_W-1:0] route_slot;
    logic routed;
    wire accepted = s_valid && s_ready;
    wire valid_key = int'(s_slot) < SLOTS;

    assign cut_lane = cut_valid && attached && lease_routed && lease_slot == cut_slot;
    assign discarded_packet = accepted && s_last && !routed;

    always_comb begin
        route_slot = attached ? lease_slot : SLOT_W'(s_slot);
        routed = 1'b0;
        s_ready = 1'b0;
        slot_valid = '0;
        slot_leased = '0;
        routed_packet = '0;

        if (attached && lease_routed)
            slot_leased[lease_slot] = 1'b1;
        if (!rst) begin
            if (attached) begin
                routed = lease_routed && !cut_lane;
                s_ready = !routed || slot_ready[lease_slot];
            end else if (!s_first || !valid_key) begin
                s_ready = 1'b1;
            end else if (!fenced[route_slot]) begin
                routed = active[route_slot];
                s_ready = !routed || slot_ready[route_slot];
            end

            if (routed)
                slot_valid[route_slot] = s_valid;
            if (routed && s_last)
                routed_packet[route_slot] = s_valid && s_ready;
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            attached <= 1'b0;
            lease_slot <= '0;
            lease_routed <= 1'b0;
        end else begin
            if (cut_lane)
                lease_routed <= 1'b0;
            if (accepted) begin
                attached <= !s_last;
                lease_routed <= routed && !s_last;
                lease_slot <= route_slot;
            end
        end
    end
endmodule

`default_nettype wire
