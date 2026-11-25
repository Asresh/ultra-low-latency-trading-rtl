`timescale 1ns/1ps
`default_nettype none
`include "replay_codes.svh"

// Two independently writable tapes share one output stream. An armed tape
// becomes immutable and starts after its release tick and prior retirement.
module banked_stream_replayer #(
    parameter integer DEPTH = 16,
    parameter integer RUN_W = 32,
    localparam integer COUNT_W = $clog2(DEPTH+1)
) (
    input  wire clk, rst,
    input  wire cfg_valid,
    output wire cfg_ready,
    input  wire [2:0] cfg_op,
    input  wire cfg_bank,
    input  wire [RUN_W-1:0] cfg_run_id,
    input  wire [31:0] cfg_tick,
    input  wire [63:0] cfg_data,
    input  wire [7:0] cfg_keep,
    input  wire cfg_last,
    input  wire [15:0] cfg_tag, cfg_repeats,
    input  wire [31:0] cfg_gap,
    input  wire [63:0] cfg_release_tick,
    output logic rsp_valid,
    input  wire rsp_ready,
    output logic [3:0] rsp_status,
    output logic [2:0] rsp_op,
    output logic rsp_bank,
    output logic [RUN_W-1:0] rsp_run_id,
    output logic [1:0] rsp_bank_state,
    output logic [COUNT_W-1:0] rsp_length,
    output logic rsp_image_open,
    output wire m_valid,
    input  wire m_ready,
    output wire [63:0] m_data,
    output wire [7:0] m_keep,
    output wire m_last,
    output wire [15:0] m_tag,
    output wire [47:0] m_due, m_offered,
    output wire [15:0] m_pass,
    output wire active, paused,
    output wire m_bank,
    output wire [RUN_W-1:0] m_run_id,
    output wire [47:0] now_tick,
    output logic [63:0] wall_tick,
    output logic run_started,
    output logic [63:0] run_start_tick,
    output logic done,
    output logic done_bank, done_stopped, done_truncated,
    output logic [RUN_W-1:0] done_run_id,
    output logic [63:0] done_tick,
    output logic [31:0] done_beats, done_packets, done_late, done_passes,
    output logic [31:0] started_runs, retired_runs, cancelled_runs, blocked_release_cycles
);
    localparam logic [2:0] CANCEL = 6, INSPECT = 7;
    localparam logic [3:0] STALE_RUN = 11, ID_EXHAUSTED = 12;
    localparam logic [1:0] WRITABLE = 0, ARMED = 1, PLAYING = 2;

    logic [COUNT_W-1:0] lengths [0:1];
    logic [1:0] image_open;
    logic pending, pending_bank;
    logic [63:0] release_tick;
    logic [RUN_W-1:0] last_run_id, pending_id, current_id;
    logic [15:0] pending_repeats;
    logic [31:0] pending_gap;
    logic owned, owner_bank, packet_open, stop_requested;

    logic waiting, host_bank, host_last;
    logic [2:0] host_op;
    logic [RUN_W-1:0] host_id;
    logic [1:0] host_state;
    wire [1:0] bank_ready, bank_rsp, bank_valid, bank_paused, bank_done;
    wire [3:0] bank_status [0:1];
    wire [COUNT_W-1:0] bank_length [0:1];
    wire [63:0] bank_data [0:1];
    wire [7:0] bank_keep [0:1];
    wire [1:0] bank_last;
    wire [15:0] bank_tag [0:1], bank_pass [0:1];
    wire [47:0] bank_due [0:1], bank_offered [0:1], bank_now [0:1];
    wire [31:0] bank_beats [0:1], bank_packets [0:1], bank_late [0:1], bank_passes [0:1];

    wire target_active = owned && owner_bank == cfg_bank;
    wire target_armed = pending && pending_bank == cfg_bank;
    wire [1:0] target_state = target_active ? PLAYING : (target_armed ? ARMED : WRITABLE);
    wire [RUN_W-1:0] target_id = target_active ? current_id : (target_armed ? pending_id : '0);
    wire request = cfg_valid && cfg_ready;
    wire host_reply = waiting && bank_rsp[host_bank];
    wire successful_host = host_reply && bank_status[host_bank] == `RP_OK;
    logic native_command;
    logic [3:0] result;

    // A retiring owner has already drained or cancelled every visible beat.
    wire retiring = owned && bank_done[owner_bank];
    wire stop_accepted = successful_host && host_op == `RP_STOP;
    wire reserve = request && result == `RP_OK && cfg_op == `RP_START;
    wire cancel = request && result == `RP_OK && cfg_op == CANCEL;
    wire due = pending && release_tick <= wall_tick;
    wire launch = !rst && due && !cancel && (!owned || retiring) && bank_ready[pending_bank];

    assign cfg_ready = !rst && !waiting && (!rsp_valid || rsp_ready) &&
                       (!native_command || bank_ready[cfg_bank]);
    assign active = !rst && owned;
    assign paused = active && bank_paused[owner_bank];
    assign m_valid = active && bank_valid[owner_bank];
    assign m_bank = active && owner_bank;
    assign m_run_id = active ? current_id : '0;
    assign m_data = m_valid ? bank_data[owner_bank] : '0;
    assign m_keep = m_valid ? bank_keep[owner_bank] : '0;
    assign m_last = m_valid && bank_last[owner_bank];
    assign m_tag = m_valid ? bank_tag[owner_bank] : '0;
    assign m_due = m_valid ? bank_due[owner_bank] : '0;
    assign m_offered = m_valid ? bank_offered[owner_bank] : '0;
    assign m_pass = m_valid ? bank_pass[owner_bank] : '0;
    assign now_tick = active ? bank_now[owner_bank] : '0;

    function automatic logic [31:0] inc32(input logic [31:0] count);
        inc32 = (&count) ? count : count + 1'b1;
    endfunction

    always_comb begin
        result = `RP_OK;
        native_command = 0;
        case (cfg_op)
            `RP_APPEND, `RP_CLEAR: begin
                if (target_state == WRITABLE)
                    native_command = 1;
                else
                    result = `RP_BUSY;
            end
            `RP_START: begin
                if (target_active || pending)
                    result = `RP_BUSY;
                else if (lengths[cfg_bank] == 0)
                    result = `RP_EMPTY;
                else if (image_open[cfg_bank])
                    result = `RP_OPEN_PACKET;
                else if (cfg_repeats == 0)
                    result = `RP_BAD_REPEAT;
                else if (&last_run_id)
                    result = ID_EXHAUSTED;
            end
            `RP_PAUSE, `RP_RESUME, `RP_STOP: begin
                if (!target_active)
                    result = `RP_BAD_STATE;
                else if (cfg_run_id != target_id)
                    result = STALE_RUN;
                else
                    native_command = 1;
            end
            CANCEL: begin
                if (!target_armed)
                    result = `RP_BAD_STATE;
                else if (cfg_run_id != target_id)
                    result = STALE_RUN;
            end
            INSPECT: begin end
            default: result = `RP_BAD_OP;
        endcase
    end

    for (genvar bank = 0; bank < 2; bank++) begin : banks
        localparam logic ID = 1'(bank);
        wire start_bank = launch && pending_bank == ID;
        wire host_command = request && native_command && cfg_bank == ID;
        wire unused_core_active;
        timestamped_stream_replayer #(.DEPTH(DEPTH)) engine (
            .clk, .rst, .cfg_valid(start_bank || host_command), .cfg_ready(bank_ready[bank]),
            .cfg_op(start_bank ? `RP_START : cfg_op), .cfg_tick, .cfg_data, .cfg_keep,
            .cfg_last, .cfg_tag, .cfg_repeats(pending_repeats), .cfg_gap(pending_gap),
            .rsp_valid(bank_rsp[bank]), .rsp_ready(1'b1),
            .rsp_status(bank_status[bank]), .rsp_length(bank_length[bank]),
            .m_valid(bank_valid[bank]), .m_ready(m_ready && active && owner_bank == ID),
            .m_data(bank_data[bank]), .m_keep(bank_keep[bank]),
            .m_last(bank_last[bank]), .m_tag(bank_tag[bank]),
            .m_due(bank_due[bank]), .m_offered(bank_offered[bank]), .m_pass(bank_pass[bank]),
            .active(unused_core_active), .paused(bank_paused[bank]), .done(bank_done[bank]),
            .now_tick(bank_now[bank]), .emitted_beats(bank_beats[bank]),
            .emitted_packets(bank_packets[bank]), .late_beats(bank_late[bank]),
            .completed_passes(bank_passes[bank])
        );
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            waiting <= 0;
            host_bank <= 0;
            host_last <= 0;
            host_op <= '0;
            host_id <= '0;
            host_state <= '0;
            lengths[0] <= '0;
            lengths[1] <= '0;
            image_open <= '0;
            rsp_valid <= 0;
            rsp_status <= '0;
            rsp_op <= '0;
            rsp_bank <= 0;
            rsp_run_id <= '0;
            rsp_bank_state <= '0;
            rsp_length <= '0;
            rsp_image_open <= 0;
        end else begin
            if (rsp_ready)
                rsp_valid <= 0;
            if (request && native_command) begin
                waiting <= 1;
                host_op <= cfg_op;
                host_bank <= cfg_bank;
                host_last <= cfg_last;
                host_id <= target_id;
                host_state <= target_state;
            end
            if (request && !native_command) begin
                rsp_valid <= 1;
                rsp_status <= result;
                rsp_op <= cfg_op;
                rsp_bank <= cfg_bank;
                rsp_run_id <= reserve ? last_run_id + 1'b1 : target_id;
                rsp_bank_state <= reserve ? ARMED : (cancel ? WRITABLE : target_state);
                rsp_length <= lengths[cfg_bank];
                rsp_image_open <= image_open[cfg_bank];
            end
            if (host_reply) begin
                waiting <= 0;
                rsp_valid <= 1;
                rsp_status <= bank_status[host_bank];
                rsp_op <= host_op;
                rsp_bank <= host_bank;
                rsp_run_id <= host_id;
                rsp_bank_state <= host_state;
                rsp_length <= bank_length[host_bank];
                rsp_image_open <= image_open[host_bank];
                if (successful_host) begin
                    lengths[host_bank] <= bank_length[host_bank];
                    if (host_op == `RP_APPEND || host_op == `RP_CLEAR) begin
                        image_open[host_bank] <= host_op == `RP_APPEND && !host_last;
                        rsp_image_open <= host_op == `RP_APPEND && !host_last;
                    end
                end
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            pending <= 0;
            pending_bank <= 0;
            pending_id <= '0;
            pending_repeats <= '0;
            pending_gap <= '0;
            release_tick <= '0;
            last_run_id <= '0;
            current_id <= '0;
            owned <= 0;
            owner_bank <= 0;
            packet_open <= 0;
            stop_requested <= 0;
            wall_tick <= '0;
            run_started <= 0;
            run_start_tick <= '0;
            done <= 0;
            done_bank <= 0;
            done_stopped <= 0;
            done_truncated <= 0;
            done_run_id <= '0;
            done_tick <= '0;
            done_beats <= '0;
            done_packets <= '0;
            done_late <= '0;
            done_passes <= '0;
            started_runs <= '0;
            retired_runs <= '0;
            cancelled_runs <= '0;
            blocked_release_cycles <= '0;
        end else begin
            if (!(&wall_tick))
                wall_tick <= wall_tick + 1'b1;
            run_started <= launch;
            done <= retiring;
            if (due && owned && !retiring && !cancel)
                blocked_release_cycles <= inc32(blocked_release_cycles);
            if (reserve) begin
                pending <= 1;
                pending_bank <= cfg_bank;
                pending_id <= last_run_id + 1'b1;
                last_run_id <= last_run_id + 1'b1;
                pending_repeats <= cfg_repeats;
                pending_gap <= cfg_gap;
                release_tick <= cfg_release_tick;
            end else if (cancel || launch) begin
                pending <= 0;
            end
            if (cancel)
                cancelled_runs <= inc32(cancelled_runs);
            if (m_valid && m_ready)
                packet_open <= !m_last;
            if (stop_accepted)
                stop_requested <= 1;
            if (retiring) begin
                owned <= 0;
                packet_open <= 0;
                stop_requested <= 0;
                done_bank <= owner_bank;
                done_run_id <= current_id;
                done_stopped <= stop_requested || stop_accepted;
                done_truncated <= packet_open;
                done_tick <= wall_tick;
                done_beats <= bank_beats[owner_bank];
                done_packets <= bank_packets[owner_bank];
                done_late <= bank_late[owner_bank];
                done_passes <= bank_passes[owner_bank];
                retired_runs <= inc32(retired_runs);
            end
            if (launch) begin
                owned <= 1;
                owner_bank <= pending_bank;
                current_id <= pending_id;
                run_start_tick <= wall_tick;
                started_runs <= inc32(started_runs);
            end
        end
    end
endmodule

`default_nettype wire
