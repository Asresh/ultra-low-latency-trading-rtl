`timescale 1ns/1ps
`default_nettype none
`include "fanout_status.svh"

// The journal can evict a sequence without reusing a slot held by a subscriber.
module multicast_replay_buffer #(
    parameter integer PORTS = 3,
    parameter integer RETAIN = 4,
    parameter integer MAX_BEATS = 8,
    parameter integer DATA_BYTES = 4,
    parameter integer SEQUENCE_W = 48,
    parameter integer EPOCH_W = 16,
    parameter integer TAG_W = 16,
    localparam integer DATA_W = 8*DATA_BYTES,
    localparam integer PORT_W = $clog2(PORTS+1),
    localparam integer SLOTS = RETAIN+PORTS+1,
    localparam integer RESIDENT_W = $clog2(SLOTS+1),
    localparam integer HIST_COUNT_W = $clog2(RETAIN+1)
) (
    input  wire clk,
    input  wire rst,
    input  wire [EPOCH_W-1:0] reset_epoch,
    input  wire [SEQUENCE_W-1:0] reset_sequence,
    input  wire cfg_valid,
    output wire cfg_ready,
    input  wire [1:0] cfg_op,
    input  wire [PORT_W-1:0] cfg_port,
    input  wire [EPOCH_W-1:0] cfg_epoch,
    input  wire [SEQUENCE_W-1:0] cfg_sequence,
    input  wire [TAG_W-1:0] cfg_tag,
    output wire cfg_report_valid,
    input  wire cfg_report_ready,
    output logic [2:0] cfg_report_status,
    output logic [1:0] cfg_report_op,
    output logic [PORT_W-1:0] cfg_report_port,
    output logic [TAG_W-1:0] cfg_report_tag,
    output logic cfg_report_enabled,
    output logic [SEQUENCE_W-1:0] cfg_report_cursor,
    input  wire s_valid,
    output wire s_ready,
    input  wire [DATA_W-1:0] s_data,
    input  wire [DATA_BYTES-1:0] s_keep,
    input  wire s_last,
    input  wire [TAG_W-1:0] s_tag,
    output wire [PORTS-1:0] m_valid,
    input  wire [PORTS-1:0] m_ready,
    output wire [PORTS-1:0] m_gap,
    output wire [PORTS-1:0] m_first,
    output wire [PORTS-1:0] m_last,
    output wire [PORTS*SEQUENCE_W-1:0] m_sequence,
    output wire [PORTS*SEQUENCE_W-1:0] m_resume,
    output wire [PORTS*DATA_W-1:0] m_data,
    output wire [PORTS*DATA_BYTES-1:0] m_keep,
    output wire [PORTS*TAG_W-1:0] m_tag,
    output wire report_valid,
    input  wire report_ready,
    output logic [2:0] report_status,
    output logic [TAG_W-1:0] report_tag,
    output logic [SEQUENCE_W-1:0] report_sequence,
    output logic [EPOCH_W-1:0] epoch,
    output logic [SEQUENCE_W-1:0] next_sequence,
    output wire [SEQUENCE_W-1:0] earliest_sequence,
    output logic [HIST_COUNT_W-1:0] retained_packets,
    output logic [RESIDENT_W-1:0] resident_slots,
    output wire exhausted,
    output wire [PORTS*32-1:0] delivered_packets,
    output wire [PORTS*32-1:0] gap_events,
    output wire [PORTS*SEQUENCE_W-1:0] lost_packets
);
    localparam integer SLOT_W = $clog2(SLOTS);
    localparam integer HIST_W = RETAIN > 1 ? $clog2(RETAIN) : 1;
    localparam integer BEAT_W = MAX_BEATS > 1 ? $clog2(MAX_BEATS) : 1;
    localparam integer SEEN_W = $clog2(MAX_BEATS+1);
    localparam integer PORT_ADDR_W = PORTS > 1 ? $clog2(PORTS) : 1;
    localparam logic [1:0] FOLLOW = 0, REPLAY = 1, DETACH = 2, QUERY = 3;
    localparam logic [1:0] IDLE = 0, DATA = 1, GAP = 2;
    localparam logic [2:0] CFG_OK = 0, BAD_PORT = 1, BAD_EPOCH = 2, BAD_FIELDS = 3,
                           FUTURE = 4, BUSY = 5;

    logic [DATA_W-1:0] data_bank [0:SLOTS-1][0:MAX_BEATS-1];
    logic [DATA_BYTES-1:0] keep_bank [0:SLOTS-1][0:MAX_BEATS-1];
    logic [BEAT_W-1:0] last_beat [0:SLOTS-1];
    logic [TAG_W-1:0] slot_tag [0:SLOTS-1];
    logic [SLOT_W-1:0] history [0:RETAIN-1];
    logic [HIST_W-1:0] history_tail;
    logic [SLOTS-1:0] retained_slots;
    logic [SLOTS-1:0] occupied_slots;
    logic [PORTS-1:0] subscribed;
    logic [1:0] mode [0:PORTS-1];
    logic [SLOT_W-1:0] pinned [0:PORTS-1];
    logic [BEAT_W-1:0] cursor [0:PORTS-1];
    logic [SEQUENCE_W-1:0] desired [0:PORTS-1], resume [0:PORTS-1];

    logic assembling, input_report_held, cfg_report_held;
    logic [SLOT_W-1:0] assembly_slot, free_slot;
    logic [SEEN_W-1:0] seen;
    logic [2:0] frame_fault;
    logic [TAG_W-1:0] ingress_tag;
    logic free_found;

    assign earliest_sequence = next_sequence - SEQUENCE_W'(retained_packets);
    assign exhausted = &next_sequence;
    assign report_valid = !rst && input_report_held;
    assign cfg_report_valid = !rst && cfg_report_held;
    assign cfg_ready = !rst && (!cfg_report_held || cfg_report_ready);
    assign s_ready = !rst && (assembling ||
        (free_found && !exhausted && (!input_report_held || report_ready)));

    always @* begin : slot_ownership
        occupied_slots = retained_slots;
        if (assembling)
            occupied_slots[assembly_slot] = 1'b1;
        for (integer port_id = 0; port_id < PORTS; port_id++)
            if (mode[port_id] == DATA)
                occupied_slots[pinned[port_id]] = 1'b1;

        free_found = 1'b0;
        free_slot = '0;
        resident_slots = '0;
        for (integer slot = SLOTS-1; slot >= 0; slot--) begin
            if (occupied_slots[slot])
                resident_slots = resident_slots+1'b1;
            else begin
                free_found = 1'b1;
                free_slot = SLOT_W'(slot);
            end
        end
    end

    wire [SLOT_W-1:0] input_slot = assembling ? assembly_slot : free_slot;
    wire [SEEN_W-1:0] beat_count = assembling ? seen : '0;
    wire [BEAT_W-1:0] write_beat = BEAT_W'(beat_count);
    wire [TAG_W-1:0] first_tag = assembling ? ingress_tag : s_tag;
    wire shape_ok = s_keep != 0 && (s_keep & (s_keep+1'b1)) == 0 && (s_last || (&s_keep));
    wire [2:0] rejection = assembling && frame_fault != `FANOUT_FORWARDED ? frame_fault :
        !shape_ok ? `FANOUT_BAD_BEAT :
        s_tag != first_tag ? `FANOUT_BAD_TAG :
        beat_count == SEEN_W'(MAX_BEATS) ? `FANOUT_TOO_LONG : `FANOUT_FORWARDED;
    wire accept_beat = s_valid && s_ready;
    wire publish = accept_beat && s_last && rejection == `FANOUT_FORWARDED;
    wire full = retained_packets == HIST_COUNT_W'(RETAIN);

    always_ff @(posedge clk) begin
        if (!rst && accept_beat && rejection == `FANOUT_FORWARDED) begin
            data_bank[input_slot][write_beat] <= s_data;
            keep_bank[input_slot][write_beat] <= s_keep;
            if (s_last) begin
                last_beat[input_slot] <= write_beat;
                slot_tag[input_slot] <= first_tag;
            end
        end
        if (!rst && publish)
            history[history_tail] <= input_slot;
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            assembling <= 0;
            assembly_slot <= '0;
            ingress_tag <= '0;
            frame_fault <= '0;
            seen <= '0;
            retained_slots <= '0;
            retained_packets <= '0;
            next_sequence <= reset_sequence;
            history_tail <= '0;
            epoch <= reset_epoch;
            input_report_held <= 0;
            {report_status,report_tag,report_sequence} <= '0;
        end else begin
            if (report_ready)
                input_report_held <= 0;
            if (accept_beat) begin
                if (s_last) begin
                    assembling <= 0;
                    input_report_held <= 1;
                    {report_status,report_tag,report_sequence} <= {rejection,first_tag,next_sequence};
                end else begin
                    assembling <= 1;
                    ingress_tag <= first_tag;
                    assembly_slot <= input_slot;
                    frame_fault <= rejection;
                    seen <= beat_count == SEEN_W'(MAX_BEATS) ? beat_count : beat_count+1'b1;
                end
            end
            if (publish) begin
                if (full)
                    retained_slots[history[history_tail]] <= 0;
                else
                    retained_packets <= retained_packets+1'b1;
                retained_slots[input_slot] <= 1;
                next_sequence <= next_sequence+1'b1;
                history_tail <= history_tail == HIST_W'(RETAIN-1) ? '0 : history_tail+1'b1;
            end
        end
    end

    wire cfg_port_valid = cfg_port < PORT_W'(PORTS);
    wire [PORT_ADDR_W-1:0] port_select = cfg_port_valid ? PORT_ADDR_W'(cfg_port) : '0;
    wire manage = cfg_valid && cfg_ready;
    logic [2:0] cfg_status;
    wire change = manage && cfg_status == CFG_OK && cfg_op != QUERY;
    always @* begin
        cfg_status = CFG_OK;
        if (!cfg_port_valid)
            cfg_status = BAD_PORT;
        else if (cfg_epoch != epoch)
            cfg_status = BAD_EPOCH;
        else if (cfg_op != REPLAY && cfg_sequence != 0)
            cfg_status = BAD_FIELDS;
        else if (cfg_op == REPLAY && cfg_sequence > next_sequence)
            cfg_status = FUTURE;
        else if (cfg_op != QUERY && mode[port_select] != IDLE)
            cfg_status = BUSY;
    end
    wire [SEQUENCE_W-1:0] target = cfg_op == FOLLOW ? next_sequence : cfg_sequence;
    always_ff @(posedge clk) begin
        if (rst) begin
            cfg_report_held <= 0;
            {cfg_report_status,cfg_report_op,cfg_report_port,cfg_report_tag,
             cfg_report_enabled,cfg_report_cursor} <= '0;
        end else if (cfg_ready) begin
            cfg_report_held <= cfg_valid;
            if (cfg_valid) begin
                {cfg_report_status,cfg_report_op,cfg_report_port,cfg_report_tag} <=
                    {cfg_status,cfg_op,cfg_port,cfg_tag};
                cfg_report_enabled <= cfg_port_valid && (change ? cfg_op != DETACH : subscribed[port_select]);
                cfg_report_cursor <= !cfg_port_valid ? '0 : (change ? target : desired[port_select]);
            end
        end
    end

    function automatic logic [HIST_W-1:0] history_index(
        input logic [HIST_W-1:0] tail,
        input logic [HIST_COUNT_W-1:0] distance
    );
        integer position;
        begin
            position = int'(tail) - int'(distance);
            if (position < 0)
                position = position+RETAIN;
            history_index = HIST_W'(position);
        end
    endfunction

    function automatic logic [31:0] increment(input logic [31:0] count);
        increment = (&count) ? count : count+1'b1;
    endfunction

    function automatic logic [DATA_W-1:0] clean(
        input logic [DATA_W-1:0] data,
        input logic [DATA_BYTES-1:0] keep
    );
        logic [DATA_W-1:0] result;
        begin
            for (integer lane = 0; lane < DATA_BYTES; lane++)
                result[8*lane+:8] = keep[lane] ? data[8*lane+:8] : '0;
            clean = result;
        end
    endfunction

    for (genvar port_id = 0; port_id < PORTS; port_id++) begin : subscriber
        localparam logic [PORT_ADDR_W-1:0] KEY = PORT_ADDR_W'(port_id);
        wire [SLOT_W-1:0] slot = pinned[port_id];
        wire [BEAT_W-1:0] beat = cursor[port_id];
        wire [DATA_BYTES-1:0] keep = keep_bank[slot][beat];
        wire [SEQUENCE_W-1:0] wanted = desired[port_id];
        wire [HIST_COUNT_W-1:0] distance = HIST_COUNT_W'(next_sequence-wanted);
        wire at_data = mode[port_id] == DATA;
        wire at_gap = mode[port_id] == GAP;
        wire consume = m_valid[port_id] && m_ready[port_id];
        logic [31:0] sent, gaps;
        logic [SEQUENCE_W-1:0] lost;
        wire [SEQUENCE_W:0] total_lost = {1'b0,lost} +
            ({1'b0,resume[port_id]} - {1'b0,wanted});

        assign m_valid[port_id] = !rst && (at_data || at_gap);
        assign m_gap[port_id] = m_valid[port_id] && at_gap;
        assign m_first[port_id] = m_valid[port_id] && (at_gap || beat == 0);
        assign m_last[port_id] = m_valid[port_id] && (at_gap || beat == last_beat[slot]);
        assign m_sequence[port_id*SEQUENCE_W+:SEQUENCE_W] = m_valid[port_id] ? wanted : '0;
        assign m_resume[port_id*SEQUENCE_W+:SEQUENCE_W] = m_gap[port_id] ? resume[port_id] : '0;
        assign m_data[port_id*DATA_W+:DATA_W] = m_valid[port_id] && at_data ?
            clean(data_bank[slot][beat],keep) : '0;
        assign m_keep[port_id*DATA_BYTES+:DATA_BYTES] = m_valid[port_id] && at_data ? keep : '0;
        assign m_tag[port_id*TAG_W+:TAG_W] = m_valid[port_id] && at_data ? slot_tag[slot] : '0;
        assign delivered_packets[port_id*32+:32] = sent;
        assign gap_events[port_id*32+:32] = gaps;
        assign lost_packets[port_id*SEQUENCE_W+:SEQUENCE_W] = lost;

        always_ff @(posedge clk) begin
            if (rst) begin
                mode[port_id] <= IDLE;
                subscribed[port_id] <= 0;
                pinned[port_id] <= '0;
                cursor[port_id] <= '0;
                desired[port_id] <= '0;
                resume[port_id] <= '0;
                {sent,gaps,lost} <= '0;
            end else if (change && port_select == KEY) begin
                subscribed[port_id] <= cfg_op != DETACH;
                desired[port_id] <= target;
            end else if (mode[port_id] == IDLE && subscribed[port_id]) begin
                if (wanted < earliest_sequence) begin
                    mode[port_id] <= GAP;
                    resume[port_id] <= earliest_sequence;
                end else if (wanted < next_sequence) begin
                    mode[port_id] <= DATA;
                    pinned[port_id] <= history[history_index(history_tail,distance)];
                    cursor[port_id] <= '0;
                end
            end else if (consume) begin
                if (at_gap) begin
                    mode[port_id] <= IDLE;
                    desired[port_id] <= resume[port_id];
                    gaps <= increment(gaps);
                    lost <= total_lost[SEQUENCE_W] ? '1 : total_lost[SEQUENCE_W-1:0];
                end else if (m_last[port_id]) begin
                    mode[port_id] <= IDLE;
                    desired[port_id] <= wanted+1'b1;
                    sent <= increment(sent);
                end else begin
                    cursor[port_id] <= beat+1'b1;
                end
            end
        end
    end

`ifdef FORMAL_REPLAY
`include "replay_properties.svh"
`endif
endmodule

`default_nettype wire
