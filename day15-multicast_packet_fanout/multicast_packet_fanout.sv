`timescale 1ns/1ps
`default_nettype none
`include "fanout_status.svh"

// Receive one packet, then enqueue a shared immutable payload on every selected
// output. Each selected output relinquishes its reference after its own last beat.
module multicast_packet_fanout #(
    parameter integer DATA_BYTES = 4,
    parameter integer PORTS = 3,
    parameter integer GROUPS = 4,
    parameter integer SLOTS = 4,
    parameter integer PORT_DEPTH = 2,
    parameter integer MAX_BEATS = 8,
    parameter integer TAG_W = 16,
    localparam integer DATA_W = DATA_BYTES * 8,
    localparam integer GROUP_W = GROUPS > 1 ? $clog2(GROUPS+1) : 1,
    localparam integer SLOT_W = SLOTS > 1 ? $clog2(SLOTS) : 1,
    localparam integer BEAT_W = MAX_BEATS > 1 ? $clog2(MAX_BEATS) : 1,
    localparam integer SEEN_W = $clog2(MAX_BEATS+1),
    localparam integer PTR_W = PORT_DEPTH > 1 ? $clog2(PORT_DEPTH) : 1,
    localparam integer COUNT_W = $clog2(PORT_DEPTH+1),
    localparam integer RESIDENT_W = $clog2(SLOTS+1)
)(
    input  wire                      clk,
    input  wire                      rst,

    input  wire                      cfg_valid,
    output wire                      cfg_ready,
    input  wire [GROUP_W-1:0]        cfg_group,
    input  wire [PORTS-1:0]          cfg_destinations,
    input  wire                      cfg_drop_on_congestion,

    input  wire                      s_valid,
    output wire                      s_ready,
    input  wire [DATA_W-1:0]         s_data,
    input  wire [DATA_BYTES-1:0]     s_keep,
    input  wire                      s_last,
    input  wire [GROUP_W-1:0]        s_group,
    input  wire [TAG_W-1:0]          s_tag,

    output wire [PORTS-1:0]          m_valid,
    input  wire [PORTS-1:0]          m_ready,
    output wire [PORTS*DATA_W-1:0]   m_data,
    output wire [PORTS*DATA_BYTES-1:0] m_keep,
    output wire [PORTS-1:0]          m_last,
    output wire [PORTS*TAG_W-1:0]    m_tag,

    output logic                    report_valid,
    input  wire                     report_ready,
    output logic [2:0]              report_status,
    output logic [TAG_W-1:0]        report_tag,
    output logic [GROUP_W-1:0]      report_group,
    output logic [PORTS-1:0]        report_destinations,
    output logic [15:0]             report_bytes,

    output logic [31:0]             forwarded_packets,
    output logic [31:0]             filtered_packets,
    output logic [31:0]             congested_packets,
    output logic [31:0]             invalid_packets,
    output wire [PORTS*32-1:0]      delivered_packets,
    output wire [PORTS*COUNT_W-1:0] port_occupancy,
    output logic [RESIDENT_W-1:0]   resident_slots
);
    localparam integer TABLE_W = GROUPS > 1 ? $clog2(GROUPS) : 1;

    logic [PORTS-1:0] route_mask [0:GROUPS-1];
    logic [GROUPS-1:0] route_drop;
    logic [DATA_W-1:0] payload [0:SLOTS-1][0:MAX_BEATS-1];
    logic [DATA_BYTES-1:0] payload_keep [0:SLOTS-1][0:MAX_BEATS-1];
    logic [TAG_W-1:0] stored_tag [0:SLOTS-1];
    logic [BEAT_W-1:0] final_index [0:SLOTS-1];

    logic [PORTS-1:0] pending [0:SLOTS-1];
    wire [PORTS-1:0] finished [0:SLOTS-1];
    wire [PORTS-1:0] no_room;

    logic packet_open, active_reserved;
    logic [SLOT_W-1:0] active_slot;
    logic [PORTS-1:0] active_mask;
    logic [GROUP_W-1:0] active_group;
    logic [TAG_W-1:0] active_tag;
    logic [2:0] active_route, active_fault;
    logic [SEEN_W-1:0] beats_seen;
    logic [15:0] bytes_seen;

    logic slot_found;
    logic [SLOT_W-1:0] candidate;
    wire group_ok = int'(s_group) < GROUPS;
    wire [TABLE_W-1:0] group_index = GROUPS == 1 ? '0 : TABLE_W'(s_group);
    wire [PORTS-1:0] offered_mask = group_ok ? route_mask[group_index] : '0;
    wire drop_policy = group_ok && route_drop[group_index];
    wire credit_ok = slot_found && !(|(offered_mask & no_room));
    wire needs_space = group_ok && (|offered_mask);
    wire can_finish_report = !report_valid || report_ready;
    wire have_start = !needs_space || credit_ok || drop_policy;

    wire [2:0] initial_route = !group_ok ? `FANOUT_BAD_GROUP :
        !needs_space ? `FANOUT_FILTERED : credit_ok ? `FANOUT_FORWARDED : `FANOUT_CONGESTED;
    wire [2:0] route_now = packet_open ? active_route : initial_route;
    wire [PORTS-1:0] chosen_mask = packet_open ? active_mask : offered_mask;
    wire [TAG_W-1:0] chosen_tag = packet_open ? active_tag : s_tag;
    wire [GROUP_W-1:0] chosen_group = packet_open ? active_group : s_group;
    wire chosen_reserved = packet_open ? active_reserved : route_now == `FANOUT_FORWARDED;
    wire [SLOT_W-1:0] chosen_slot = packet_open ? active_slot : candidate;
    wire [SEEN_W-1:0] incoming_index = packet_open ? beats_seen : '0;
    wire [BEAT_W-1:0] memory_index = BEAT_W'(incoming_index);
    wire [2:0] prior_fault = packet_open ? active_fault : `FANOUT_FORWARDED;
    wire shape_ok = (s_keep != 0) && ((s_keep & (s_keep + 1'b1)) == 0) &&
                    (s_last || (&s_keep));
    wire [2:0] fault_now = prior_fault != `FANOUT_FORWARDED ? prior_fault :
        !shape_ok ? `FANOUT_BAD_BEAT :
        packet_open && s_tag != active_tag ? `FANOUT_BAD_TAG :
        incoming_index == SEEN_W'(MAX_BEATS) ? `FANOUT_TOO_LONG : `FANOUT_FORWARDED;
    wire [2:0] final_status = route_now == `FANOUT_BAD_GROUP ? route_now :
        fault_now == `FANOUT_FORWARDED ? route_now : fault_now;
    wire ingress = s_valid && s_ready;
    wire committed = ingress && s_last && final_status == `FANOUT_FORWARDED;

    assign cfg_ready = !rst;
    assign s_ready = !rst && (packet_open || (can_finish_report && have_start));

    // Select the lowest free slot. The currently assembling packet is live
    // even before its completed payload has any egress references.
    always @* begin
        candidate = '0;
        slot_found = 1'b0;
        resident_slots = '0;
        for (integer index = SLOTS-1; index >= 0; index = index-1) begin
            if ((|pending[index]) || (packet_open && active_reserved && active_slot == SLOT_W'(index)))
                resident_slots = resident_slots + 1'b1;
            else begin
                candidate = SLOT_W'(index);
                slot_found = 1'b1;
            end
        end
    end

    // Return the number of physically selected input byte lanes.
    function automatic logic [15:0] lane_count(input logic [DATA_BYTES-1:0] keep);
        logic [15:0] count;
        begin
            count = 0;
            for (integer index = 0; index < DATA_BYTES; index++)
                count = count + 16'(keep[index]);
            lane_count = count;
        end
    endfunction

    // Saturate the externally visible byte count when a malformed frame is long.
    function automatic logic [15:0] count_bytes(input logic [15:0] previous, increment);
        logic [16:0] sum;
        begin
            sum = {1'b0, previous} + {1'b0, increment};
            count_bytes = sum[16] ? '1 : sum[15:0];
        end
    endfunction

    // Count each event once without wrapping the externally readable statistic.
    function automatic logic [31:0] increment_count(input logic [31:0] previous);
        increment_count = (&previous) ? previous : previous + 1'b1;
    endfunction

    // Wrap a queue pointer even when the descriptor queue depth is not a power of two.
    function automatic logic [PTR_W-1:0] next_pointer(input logic [PTR_W-1:0] value);
        next_pointer = value == PTR_W'(PORT_DEPTH-1) ? '0 : value + 1'b1;
    endfunction

    // Mask invalid lanes so poisoned final-beat padding never leaves a port.
    function automatic logic [DATA_W-1:0] clear_padding(
        input logic [DATA_W-1:0] data,
        input logic [DATA_BYTES-1:0] keep
    );
        logic [DATA_W-1:0] cleaned;
        begin
            cleaned = '0;
            for (integer index = 0; index < DATA_BYTES; index++)
                if (keep[index])
                    cleaned[8*index+:8] = data[8*index+:8];
            clear_padding = cleaned;
        end
    endfunction

    wire [15:0] next_bytes = count_bytes(packet_open ? bytes_seen : 16'd0, lane_count(s_keep));

    // Route configuration and ingress use pre-edge semantics. A simultaneous
    // change takes effect for a packet whose first beat transfers later.
    always_ff @(posedge clk) begin
        if (rst) begin
            for (integer group_id = 0; group_id < GROUPS; group_id++)
                route_mask[group_id] <= '0;
            route_drop <= '0;
        end else if (cfg_valid && cfg_ready && int'(cfg_group) < GROUPS) begin
            route_mask[GROUPS == 1 ? '0 : TABLE_W'(cfg_group)] <= cfg_destinations;
            route_drop[GROUPS == 1 ? '0 : TABLE_W'(cfg_group)] <= cfg_drop_on_congestion;
        end
    end

    // Store candidate beats without publishing descriptors. An invalid final
    // decision leaves no live references to any bytes previously written.
    always_ff @(posedge clk) begin
        if (ingress && chosen_reserved && fault_now == `FANOUT_FORWARDED) begin
            payload[chosen_slot][memory_index] <= s_data;
            payload_keep[chosen_slot][memory_index] <= s_keep;
        end
        if (committed) begin
            stored_tag[chosen_slot] <= chosen_tag;
            final_index[chosen_slot] <= memory_index;
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            packet_open <= 1'b0;
            active_reserved <= 1'b0;
            report_valid <= 1'b0;
            forwarded_packets <= '0;
            filtered_packets <= '0;
            congested_packets <= '0;
            invalid_packets <= '0;
        end else begin
            if (report_ready)
                report_valid <= 1'b0;
            if (ingress && s_last) begin
                packet_open <= 1'b0;
                active_reserved <= 1'b0;
                report_valid <= 1'b1;
                report_tag <= chosen_tag;
                report_group <= chosen_group;
                report_destinations <= chosen_mask;
                report_bytes <= next_bytes;
                report_status <= final_status;
                case (final_status)
                    `FANOUT_FORWARDED: forwarded_packets <= increment_count(forwarded_packets);
                    `FANOUT_FILTERED: filtered_packets <= increment_count(filtered_packets);
                    `FANOUT_CONGESTED: congested_packets <= increment_count(congested_packets);
                    default: invalid_packets <= increment_count(invalid_packets);
                endcase
            end else if (ingress) begin
                packet_open <= 1'b1;
                active_reserved <= chosen_reserved;
                active_slot <= chosen_slot;
                active_mask <= chosen_mask;
                active_group <= chosen_group;
                active_tag <= chosen_tag;
                active_route <= route_now;
                active_fault <= fault_now;
                bytes_seen <= next_bytes;
                beats_seen <= incoming_index == SEEN_W'(MAX_BEATS) ? incoming_index :
                              incoming_index + 1'b1;
            end
        end
    end

    for (genvar bank = 0; bank < SLOTS; bank++) begin : g_slot
        // A completion clears only its port's bit. Several outputs may clear
        // the same slot on one edge, while allocation uses pre-edge ownership.
        always_ff @(posedge clk) begin
            if (rst)
                pending[bank] <= '0;
            else if (committed && chosen_slot == SLOT_W'(bank))
                pending[bank] <= chosen_mask;
            else
                pending[bank] <= pending[bank] & ~finished[bank];
        end
    end

    for (genvar port_id = 0; port_id < PORTS; port_id++) begin : g_port
        logic [SLOT_W-1:0] queue [0:PORT_DEPTH-1];
        logic [PTR_W-1:0] head, tail;
        logic [COUNT_W-1:0] count;
        logic [BEAT_W-1:0] cursor;
        logic [31:0] sent_count;
        wire [SLOT_W-1:0] slot = queue[head];
        wire enqueue = committed && chosen_mask[port_id];
        wire pop_beat = m_valid[port_id] && m_ready[port_id];
        wire pop_packet = pop_beat && m_last[port_id];
        wire [DATA_BYTES-1:0] keep = payload_keep[slot][cursor];

        assign m_valid[port_id] = !rst && count != 0;
        assign m_last[port_id] = m_valid[port_id] && cursor == final_index[slot];
        assign m_data[port_id*DATA_W+:DATA_W] =
            m_valid[port_id] ? clear_padding(payload[slot][cursor], keep) : '0;
        assign m_keep[port_id*DATA_BYTES+:DATA_BYTES] = m_valid[port_id] ? keep : '0;
        assign m_tag[port_id*TAG_W+:TAG_W] = m_valid[port_id] ? stored_tag[slot] : '0;
        assign port_occupancy[port_id*COUNT_W+:COUNT_W] = count;
        assign delivered_packets[port_id*32+:32] = sent_count;
        assign no_room[port_id] = count == COUNT_W'(PORT_DEPTH);

        for (genvar bank = 0; bank < SLOTS; bank++) begin : g_finish
            assign finished[bank][port_id] = pop_packet && slot == SLOT_W'(bank);
        end

        always_ff @(posedge clk) begin
            if (rst) begin
                head <= '0;
                tail <= '0;
                count <= '0;
                cursor <= '0;
                sent_count <= '0;
            end else begin
                if (enqueue) begin
                    queue[tail] <= chosen_slot;
                    tail <= next_pointer(tail);
                end
                if (pop_packet) begin
                    head <= next_pointer(head);
                    cursor <= '0;
                    sent_count <= increment_count(sent_count);
                end else if (pop_beat)
                    cursor <= cursor + 1'b1;
                case ({enqueue, pop_packet})
                    2'b10: count <= count + 1'b1;
                    2'b01: count <= count - 1'b1;
                    default: ;
                endcase
            end
        end
    end
endmodule

`default_nettype wire
