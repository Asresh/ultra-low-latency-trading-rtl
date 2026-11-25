`timescale 1ns/1ps
`default_nettype none

module multisession_market_data #(
    parameter integer SLOTS = 3,
    parameter integer PACKET_BYTES = 256,
    parameter integer MESSAGE_BYTES = 64,
    parameter integer BLOCKS = 16,
    parameter integer WINDOW = 8,
    parameter integer GAP_TICKS = 32,
    parameter integer REQUEST_MAX = 8,
    parameter integer EPOCH_W = 16,
    parameter integer WAIT_W = 16,
    localparam integer SLOT_W = SLOTS > 1 ? $clog2(SLOTS) : 1,
    localparam integer KEY_W = $clog2(SLOTS + 1)
) (
    input  wire                     clk,
    input  wire                     rst,

    input  wire                     cfg_valid,
    output wire                     cfg_ready,
    input  wire [KEY_W-1:0]         cfg_slot,
    input  wire [EPOCH_W-1:0]        cfg_expected_epoch,
    input  wire                     cfg_enable,
    input  wire                     cfg_force,
    input  wire [79:0]              cfg_session,
    input  wire [63:0]              cfg_sequence,
    input  wire [WAIT_W-1:0]        cfg_request_spacing,

    output wire                     ack_valid,
    input  wire                     ack_ready,
    output wire [1:0]              ack_status,
    output wire [KEY_W-1:0]         ack_slot,
    output wire [EPOCH_W-1:0]        ack_epoch,
    output wire                     ack_active,
    output wire [1:0]              ack_cut_lanes,

    input  wire [1:0]              feed_valid,
    output wire [1:0]              feed_ready,
    input  wire [127:0]            feed_data,
    input  wire [15:0]             feed_keep,
    input  wire [1:0]              feed_first,
    input  wire [1:0]              feed_last,
    input  wire [2*KEY_W-1:0]       feed_slots,

    output wire                     event_valid,
    input  wire                     event_ready,
    output wire [SLOT_W-1:0]        event_slot,
    output wire [EPOCH_W-1:0]        event_epoch,
    output wire [587:0]            event_data,
    output wire                     request_valid,
    input  wire                     request_ready,
    output wire [SLOT_W-1:0]        request_slot,
    output wire [EPOCH_W-1:0]        request_epoch,
    output wire [159:0]            request_data,

    input  wire [KEY_W-1:0]         monitor_slot,
    input  wire                     monitor_snapshot,
    input  wire [3:0]              monitor_index,
    output wire                     monitor_valid,
    output wire                     monitor_active,
    output wire                     monitor_fenced,
    output wire [EPOCH_W-1:0]        monitor_epoch,
    output wire [79:0]              monitor_session,
    output wire [63:0]              monitor_expected,
    output wire [63:0]              monitor_high_water,
    output wire                     monitor_gap,
    output wire                     monitor_end,
    output wire [5:0]              monitor_occupancy,
    output wire [31:0]             monitor_statistic,
    output wire [5:0]              monitor_peak,
    output wire [WAIT_W-1:0]        monitor_cooldown,
    output wire [63:0]             monitor_packets,
    output wire [31:0]             monitor_events,
    output wire [31:0]             monitor_requests,
    output logic [63:0]            discarded_packets
);
    localparam integer EVENT_W = 588 + EPOCH_W;
    localparam integer REQUEST_W = 160 + EPOCH_W;
    wire [SLOTS-1:0] active, fenced, install, install_ready;
    wire [SLOTS*EPOCH_W-1:0] epochs;
    wire [SLOTS*WAIT_W-1:0] request_spacing;
    wire [79:0] install_session;
    wire [63:0] install_sequence;
    wire cut_valid;
    wire [SLOT_W-1:0] cut_slot;
    wire [1:0] cut_lanes, discarded;

    wire [SLOTS-1:0] lane_valid [0:1], lane_ready [0:1];
    wire [SLOTS-1:0] lane_leases [0:1], lane_packets [0:1];
    wire [SLOTS-1:0] exported;
    wire ack_buffered, event_buffered, request_buffered;
    wire [SLOTS-1:0] event_eligible, request_eligible;
    wire [SLOTS-1:0] core_event_ready, core_request_ready;
    wire [SLOTS*EVENT_W-1:0] event_items;
    wire [SLOTS*REQUEST_W-1:0] request_items;

    logic [79:0] sessions [0:SLOTS-1];
    logic [WAIT_W-1:0] cooldowns [0:SLOTS-1];
    logic [31:0] a_total [0:SLOTS-1], b_total [0:SLOTS-1];
    logic [31:0] e_total [0:SLOTS-1], r_total [0:SLOTS-1];
    logic [63:0] saved_packets [0:SLOTS-1];
    logic [31:0] saved_events [0:SLOTS-1], saved_requests [0:SLOTS-1];
    wire [63:0] expected [0:SLOTS-1], high_water [0:SLOTS-1];
    wire [SLOTS-1:0] gaps, ended;
    wire [5:0] occupancy [0:SLOTS-1], peak [0:SLOTS-1];
    wire [31:0] statistic [0:SLOTS-1];

    wire index_ok = int'(monitor_slot) < SLOTS;
    wire [SLOT_W-1:0] monitor_index_slot = index_ok ? SLOT_W'(monitor_slot) : '0;
    wire current_visible = monitor_active;

    assign event_valid = !rst && event_buffered;
    assign request_valid = !rst && request_buffered;
    assign ack_valid = !rst && ack_buffered;
    assign monitor_valid = !rst && index_ok;
    assign monitor_active = monitor_valid && active[monitor_index_slot];
    assign monitor_fenced = monitor_valid && fenced[monitor_index_slot];
    assign monitor_epoch = monitor_valid ? epochs[monitor_index_slot*EPOCH_W+:EPOCH_W] : '0;
    assign monitor_session = monitor_active ? sessions[monitor_index_slot] : '0;
    assign monitor_expected = current_visible ? expected[monitor_index_slot] : '0;
    assign monitor_high_water = current_visible ? high_water[monitor_index_slot] : '0;
    assign monitor_gap = current_visible && gaps[monitor_index_slot];
    assign monitor_end = current_visible && ended[monitor_index_slot];
    assign monitor_occupancy = current_visible ? occupancy[monitor_index_slot] : '0;
    assign monitor_statistic = current_visible ? statistic[monitor_index_slot] : '0;
    assign monitor_peak = current_visible ? peak[monitor_index_slot] : '0;
    assign monitor_cooldown = current_visible ? cooldowns[monitor_index_slot] : '0;
    assign monitor_packets = current_visible ? saved_packets[monitor_index_slot] : '0;
    assign monitor_events = current_visible ? saved_events[monitor_index_slot] : '0;
    assign monitor_requests = current_visible ? saved_requests[monitor_index_slot] : '0;

    // Traffic statistics saturate rather than presenting a misleading wrap.
    function automatic logic [31:0] saturating_increment(input logic [31:0] value);
        saturating_increment = (&value) ? value : value + 1'b1;
    endfunction

    mold_session_config #(.SLOTS(SLOTS), .EPOCH_W(EPOCH_W), .WAIT_W(WAIT_W)) control (
        .clk, .rst, .cfg_valid, .cfg_ready, .cfg_slot, .cfg_expected_epoch,
        .cfg_enable, .cfg_force, .cfg_session, .cfg_sequence, .cfg_request_spacing,
        .ack_valid(ack_buffered), .ack_ready, .ack_status, .ack_slot, .ack_epoch, .ack_active,
        .ack_cut_lanes,
        .leased(lane_leases[0] | lane_leases[1]), .output_pending(exported), .install_ready,
        .active, .epochs, .request_spacing, .fenced, .install,
        .install_session, .install_sequence, .cut_valid, .cut_slot, .cut_lanes
    );

    for (genvar lane = 0; lane < 2; lane++) begin : demultiplex
        mold_session_ingress #(.SLOTS(SLOTS)) ingress (
            .clk, .rst, .active, .fenced, .cut_valid, .cut_slot, .cut_lane(cut_lanes[lane]),
            .s_valid(feed_valid[lane]), .s_ready(feed_ready[lane]), .s_first(feed_first[lane]),
            .s_last(feed_last[lane]), .s_slot(feed_slots[lane*KEY_W+:KEY_W]),
            .slot_valid(lane_valid[lane]), .slot_ready(lane_ready[lane]),
            .slot_leased(lane_leases[lane]), .routed_packet(lane_packets[lane]),
            .discarded_packet(discarded[lane])
        );

        always_ff @(posedge clk) begin
            if (rst)
                discarded_packets[lane*32+:32] <= '0;
            else if (discarded[lane])
                discarded_packets[lane*32+:32] <=
                    saturating_increment(discarded_packets[lane*32+:32]);
        end
    end

    mold_session_mux #(.SLOTS(SLOTS), .WIDTH(EVENT_W)) event_arbiter (
        .clk, .rst, .s_valid(event_eligible), .s_ready(core_event_ready), .s_data(event_items),
        .m_valid(event_buffered), .m_ready(event_ready), .m_slot(event_slot),
        .m_data({event_epoch,event_data})
    );
    mold_session_mux #(.SLOTS(SLOTS), .WIDTH(REQUEST_W)) request_arbiter (
        .clk, .rst, .s_valid(request_eligible), .s_ready(core_request_ready), .s_data(request_items),
        .m_valid(request_buffered), .m_ready(request_ready), .m_slot(request_slot),
        .m_data({request_epoch,request_data})
    );

    for (genvar slot = 0; slot < SLOTS; slot++) begin : receivers
        wire one_event_valid, one_request_valid;
        wire [587:0] one_event;
        wire [159:0] one_request;
        wire e_pending = event_buffered && event_slot == SLOT_W'(slot);
        wire r_pending = request_buffered && request_slot == SLOT_W'(slot);
        wire snapshot_selected = monitor_snapshot && monitor_valid &&
                                 monitor_index_slot == SLOT_W'(slot);
        wire [EPOCH_W-1:0] epoch = epochs[slot*EPOCH_W+:EPOCH_W];

        assign exported[slot] = e_pending || r_pending;
        assign event_eligible[slot] = active[slot] && !fenced[slot] && one_event_valid;
        assign request_eligible[slot] = active[slot] && !fenced[slot] && one_request_valid &&
                                        cooldowns[slot] == 0 && !r_pending;
        assign event_items[slot*EVENT_W+:EVENT_W] = {epoch,one_event};
        assign request_items[slot*REQUEST_W+:REQUEST_W] = {epoch,one_request};

        redundant_market_data #(
            .PACKET_BYTES(PACKET_BYTES), .MESSAGE_BYTES(MESSAGE_BYTES), .BLOCKS(BLOCKS),
            .WINDOW(WINDOW), .GAP_TICKS(GAP_TICKS), .REQUEST_MAX(REQUEST_MAX)
        ) receiver (
            .clk, .rst, .cfg_valid(install[slot]), .cfg_ready(install_ready[slot]),
            .cfg_session(install_session), .cfg_sequence(install_sequence),
            .feed_valid({lane_valid[1][slot],lane_valid[0][slot]}),
            .feed_ready({lane_ready[1][slot],lane_ready[0][slot]}),
            .feed_data, .feed_keep, .feed_first, .feed_last,
            .event_valid(one_event_valid), .event_ready(core_event_ready[slot]), .event_data(one_event),
            .request_valid(one_request_valid), .request_ready(core_request_ready[slot]),
            .request_data(one_request), .expected_sequence(expected[slot]),
            .high_water(high_water[slot]), .gap_pending(gaps[slot]), .session_end_seen(ended[slot]),
            .reorder_occupancy(occupancy[slot]), .stats_snapshot(snapshot_selected),
            .stats_index(monitor_index), .stats_data(statistic[slot]), .stats_peak(peak[slot])
        );

        // Install resets only the selected context's telemetry.
        always_ff @(posedge clk) begin
            if (rst || install[slot]) begin
                sessions[slot] <= rst ? '0 : install_session;
                cooldowns[slot] <= '0;
                a_total[slot] <= '0;
                b_total[slot] <= '0;
                e_total[slot] <= '0;
                r_total[slot] <= '0;
                saved_packets[slot] <= '0;
                saved_events[slot] <= '0;
                saved_requests[slot] <= '0;
            end else begin
                if (cooldowns[slot] != 0)
                    cooldowns[slot] <= cooldowns[slot] - 1'b1;
                if (r_pending && request_valid && request_ready) begin
                    cooldowns[slot] <= request_spacing[slot*WAIT_W+:WAIT_W];
                    r_total[slot] <= saturating_increment(r_total[slot]);
                end
                if (e_pending && event_valid && event_ready)
                    e_total[slot] <= saturating_increment(e_total[slot]);
                if (lane_packets[0][slot])
                    a_total[slot] <= saturating_increment(a_total[slot]);
                if (lane_packets[1][slot])
                    b_total[slot] <= saturating_increment(b_total[slot]);
                if (snapshot_selected) begin
                    saved_packets[slot] <= {b_total[slot],a_total[slot]};
                    saved_events[slot] <= e_total[slot];
                    saved_requests[slot] <= r_total[slot];
                end
            end
        end
    end
endmodule

`default_nettype wire
