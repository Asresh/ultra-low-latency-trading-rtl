`timescale 1ns/1ps
`default_nettype none
`include "fanout_status.svh"

// Per-subscriber mandatory and optional policy ahead of the shared payload bank.
module multicast_policy_fanout #(
    parameter integer DATA_BYTES = 4,
    parameter integer PORTS = 3,
    parameter integer GROUPS = 4,
    parameter integer SLOTS = 4,
    parameter integer PORT_DEPTH = 2,
    parameter integer MAX_BEATS = 8,
    parameter integer TAG_W = 16,
    parameter integer EPOCH_W = 16,
    localparam integer DATA_W = DATA_BYTES * 8,
    localparam integer GROUP_W = $clog2(GROUPS+1),
    localparam integer COUNT_W = $clog2(PORT_DEPTH+1),
    localparam integer RESIDENT_W = $clog2(SLOTS+1)
)(
    input  wire clk,
    input  wire rst,
    input  wire cfg_valid,
    output wire cfg_ready,
    input  wire [GROUP_W-1:0] cfg_group,
    input  wire [PORTS-1:0] cfg_requested,
    input  wire [PORTS-1:0] cfg_mandatory,
    input  wire [PORTS*COUNT_W-1:0] cfg_optional_marks,
    input  wire cfg_drop_on_congestion,
    output logic cfg_ack,
    output logic cfg_ok,
    input  wire s_valid,
    output wire s_ready,
    input  wire [DATA_W-1:0] s_data,
    input  wire [DATA_BYTES-1:0] s_keep,
    input  wire s_last,
    input  wire [GROUP_W-1:0] s_group,
    input  wire [TAG_W-1:0] s_tag,
    output wire [PORTS-1:0] m_valid,
    input  wire [PORTS-1:0] m_ready,
    output wire [PORTS*DATA_W-1:0] m_data,
    output wire [PORTS*DATA_BYTES-1:0] m_keep,
    output wire [PORTS-1:0] m_last,
    output wire [PORTS*TAG_W-1:0] m_tag,
    output wire report_valid,
    input  wire report_ready,
    output logic [2:0] report_status,
    output wire [TAG_W-1:0] report_tag,
    output logic [GROUP_W-1:0] report_group,
    output logic [EPOCH_W-1:0] report_epoch,
    output logic [PORTS-1:0] report_requested,
    output logic [PORTS-1:0] report_mandatory,
    output logic [PORTS-1:0] report_no_credit,
    output logic report_no_slot,
    output wire [PORTS-1:0] report_enqueued,
    output wire [15:0] report_bytes,
    output wire [PORTS*COUNT_W-1:0] port_occupancy,
    output wire [RESIDENT_W-1:0] resident_slots,
    output wire [PORTS*32-1:0] delivered_packets,
    output wire [PORTS*32-1:0] omitted_optional_packets
);
    localparam integer GROUP_ADDR_W = GROUPS > 1 ? $clog2(GROUPS) : 1;
    localparam logic [2:0] PARTIAL = 3'd7;
    localparam logic [1:0] WAIT_FIRST = 0, INJECT_FIRST = 1, COPY_FRAME = 2, WAIT_REPORT = 3;

    logic [1:0] state;
    logic [PORTS-1:0] requested_table [0:GROUPS-1], mandatory_table [0:GROUPS-1];
    logic [PORTS*COUNT_W-1:0] marks_table [0:GROUPS-1];
    logic [GROUPS-1:0] drop_table;
    logic [EPOCH_W-1:0] epochs [0:GROUPS-1];

    logic [DATA_W-1:0] first_data;
    logic [DATA_BYTES-1:0] first_keep;
    logic first_last;
    logic [TAG_W-1:0] first_tag;
    logic [2:0] planned_status;
    wire cfg_group_valid = cfg_group < GROUP_W'(GROUPS);
    wire group_valid = s_group < GROUP_W'(GROUPS);
    wire [GROUP_ADDR_W-1:0] cfg_index = GROUP_ADDR_W'(cfg_group);
    wire [GROUP_ADDR_W-1:0] input_index = GROUP_ADDR_W'(s_group);
    wire [PORTS-1:0] requested = group_valid ? requested_table[input_index] : '0;
    wire [PORTS-1:0] mandatory = group_valid ? mandatory_table[input_index] : '0;
    wire [PORTS*COUNT_W-1:0] marks = group_valid ? marks_table[input_index] : '0;
    wire may_drop = group_valid && drop_table[input_index];
    wire [PORTS-1:0] no_credit, invalid_mark;
    wire no_slot = resident_slots == RESIDENT_W'(SLOTS);
    wire room_for_mandatory = !(|(mandatory & no_credit)) && (!no_slot || mandatory == 0);
    wire [PORTS-1:0] selected = no_slot || !room_for_mandatory ? '0 : requested & ~no_credit;
    wire accept_first = s_ready && s_valid && state == WAIT_FIRST;
    wire acceptable_config = cfg_group_valid && (cfg_mandatory & ~cfg_requested) == 0 && !(|invalid_mark);

    assign cfg_ready = !rst;
    for (genvar port_id = 0; port_id < PORTS; port_id++) begin : subscriber
        wire [COUNT_W-1:0] mark = cfg_optional_marks[port_id*COUNT_W+:COUNT_W];
        wire optional_port = cfg_requested[port_id] && !cfg_mandatory[port_id];
        wire [COUNT_W-1:0] capacity = mandatory[port_id] ? COUNT_W'(PORT_DEPTH) :
            marks[port_id*COUNT_W+:COUNT_W];
        logic [31:0] skipped;
        assign invalid_mark[port_id] = optional_port ? mark > COUNT_W'(PORT_DEPTH) : mark != 0;
        assign no_credit[port_id] = requested[port_id] &&
            port_occupancy[port_id*COUNT_W+:COUNT_W] >= capacity;
        assign omitted_optional_packets[port_id*32+:32] = skipped;
        always_ff @(posedge clk) begin
            if (rst)
                skipped <= '0;
            else if (report_valid && report_ready && (report_status == PARTIAL ||
                     report_status == `FANOUT_CONGESTED) && report_requested[port_id] &&
                     !report_mandatory[port_id] && !report_enqueued[port_id] && !(&skipped))
                skipped <= skipped + 1'b1;
        end
    end

    logic [2:0] candidate_status;
    always @* begin
        if (!group_valid)
            candidate_status = `FANOUT_BAD_GROUP;
        else if (requested == 0)
            candidate_status = `FANOUT_FILTERED;
        else if (selected == 0)
            candidate_status = `FANOUT_CONGESTED;
        else
            candidate_status = selected == requested ? `FANOUT_FORWARDED : PARTIAL;
    end

    wire stream_valid = state == INJECT_FIRST || (state == COPY_FRAME && s_valid);
    wire stream_ready, core_report_valid, unused_core_cfg_ready;
    wire [2:0] core_status;
    wire [PORTS-1:0] core_destinations;
    wire [0:0] unused_core_group;
    wire [31:0] unused_forwarded, unused_filtered, unused_congested, unused_invalid;
    assign s_ready = !rst && ((state == WAIT_FIRST && (room_for_mandatory || may_drop)) ||
                              (state == COPY_FRAME && stream_ready));

    assign report_valid = !rst && state == WAIT_REPORT && core_report_valid;
    always @* begin
        if (planned_status == `FANOUT_BAD_GROUP)
            report_status = `FANOUT_BAD_GROUP;
        else if (core_status >= `FANOUT_BAD_BEAT)
            report_status = core_status;
        else
            report_status = planned_status;
    end
    assign report_enqueued = report_status == `FANOUT_FORWARDED || report_status == PARTIAL ?
        core_destinations : '0;

    // One private route in the existing core holds the selected mask. The
    // extra staging edge programs that mask before injecting the first beat.
    multicast_packet_fanout #(
        .DATA_BYTES(DATA_BYTES), .PORTS(PORTS), .GROUPS(1), .SLOTS(SLOTS),
        .PORT_DEPTH(PORT_DEPTH), .MAX_BEATS(MAX_BEATS), .TAG_W(TAG_W)
    ) payload_bank (
        .clk, .rst, .cfg_valid(accept_first), .cfg_ready(unused_core_cfg_ready), .cfg_group(1'b0),
        .cfg_destinations(selected), .cfg_drop_on_congestion(1'b0),
        .s_valid(stream_valid), .s_ready(stream_ready),
        .s_data(state == INJECT_FIRST ? first_data : s_data),
        .s_keep(state == INJECT_FIRST ? first_keep : s_keep),
        .s_last(state == INJECT_FIRST ? first_last : s_last),
        .s_group(1'b0), .s_tag(state == INJECT_FIRST ? first_tag : s_tag),
        .m_valid, .m_ready, .m_data, .m_keep, .m_last, .m_tag,
        .report_valid(core_report_valid), .report_ready(report_ready && state == WAIT_REPORT),
        .report_status(core_status), .report_tag, .report_group(unused_core_group),
        .report_destinations(core_destinations), .report_bytes,
        .forwarded_packets(unused_forwarded), .filtered_packets(unused_filtered),
        .congested_packets(unused_congested), .invalid_packets(unused_invalid),
        .delivered_packets, .port_occupancy, .resident_slots
    );

    always_ff @(posedge clk) begin
        if (rst) begin
            for (integer index = 0; index < GROUPS; index++) begin
                requested_table[index] <= '0;
                mandatory_table[index] <= '0;
                marks_table[index] <= '0;
                epochs[index] <= '0;
            end
            drop_table <= '0;
            cfg_ack <= 0;
            cfg_ok <= 0;
        end else begin
            cfg_ack <= cfg_valid && cfg_ready;
            cfg_ok <= cfg_valid && cfg_ready && acceptable_config;
            if (cfg_valid && cfg_ready && acceptable_config) begin
                requested_table[cfg_index] <= cfg_requested;
                mandatory_table[cfg_index] <= cfg_mandatory;
                marks_table[cfg_index] <= cfg_optional_marks;
                drop_table[cfg_index] <= cfg_drop_on_congestion;
                epochs[cfg_index] <= epochs[cfg_index] + 1'b1;
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= WAIT_FIRST;
            first_data <= '0;
            first_keep <= '0;
            first_last <= 0;
            first_tag <= '0;
            report_group <= '0;
            report_epoch <= '0;
            report_requested <= '0;
            report_mandatory <= '0;
            report_no_credit <= '0;
            report_no_slot <= 0;
            planned_status <= `FANOUT_FILTERED;
        end else begin
            case (state)
                WAIT_FIRST: if (accept_first) begin
                    first_data <= s_data;
                    first_keep <= s_keep;
                    first_last <= s_last;
                    first_tag <= s_tag;
                    report_group <= s_group;
                    report_epoch <= group_valid ? epochs[input_index] : '0;
                    report_requested <= requested;
                    report_mandatory <= mandatory;
                    report_no_credit <= no_credit;
                    report_no_slot <= (|requested) && no_slot;
                    planned_status <= candidate_status;
                    state <= INJECT_FIRST;
                end
                INJECT_FIRST: if (stream_ready)
                    state <= first_last ? WAIT_REPORT : COPY_FRAME;
                COPY_FRAME: if (s_valid && s_ready && s_last)
                    state <= WAIT_REPORT;
                WAIT_REPORT: if (report_valid && report_ready)
                    state <= WAIT_FIRST;
                default: state <= WAIT_FIRST;
            endcase
        end
    end
endmodule

`default_nettype wire
