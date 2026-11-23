`timescale 1ns/1ps
`default_nettype none
`include "replay_codes.svh"

// Store a timestamped stream once, then replay it at one event per clock.
module timestamped_stream_replayer #(
    parameter integer DEPTH = 16,
    localparam integer INDEX_W = (DEPTH > 1) ? $clog2(DEPTH) : 1,
    localparam integer COUNT_W = $clog2(DEPTH+1)
) (
    input  wire                  clk,
    input  wire                  rst,
    input  wire                  cfg_valid,
    output wire                  cfg_ready,
    input  wire [2:0]            cfg_op,
    input  wire [31:0]           cfg_tick,
    input  wire [63:0]           cfg_data,
    input  wire [7:0]            cfg_keep,
    input  wire                  cfg_last,
    input  wire [15:0]           cfg_tag,
    input  wire [15:0]           cfg_repeats,
    input  wire [31:0]           cfg_gap,
    output logic                 rsp_valid,
    input  wire                  rsp_ready,
    output logic [3:0]           rsp_status,
    output logic [COUNT_W-1:0]   rsp_length,
    output logic                 m_valid,
    input  wire                  m_ready,
    output logic [63:0]          m_data,
    output logic [7:0]           m_keep,
    output logic                 m_last,
    output logic [15:0]          m_tag,
    output logic [47:0]          m_due,
    output logic [47:0]          m_offered,
    output logic [15:0]          m_pass,
    output logic                 active,
    output logic                 paused,
    output logic                 done,
    output logic [47:0]          now_tick,
    output logic [31:0]          emitted_beats,
    output logic [31:0]          emitted_packets,
    output logic [31:0]          late_beats,
    output logic [31:0]          completed_passes
);
    localparam integer EVENT_W = 121;
    logic [COUNT_W-1:0] loaded;
    logic [31:0] latest;
    logic image_open;
    logic [15:0] image_tag;
    logic [15:0] pass_number, pass_limit;
    logic [31:0] repeat_gap;
    logic [47:0] pass_base;
    logic [COUNT_W-1:0] next_index;
    logic stop_pending;

    wire req = cfg_valid && cfg_ready;
    wire deliver = m_valid && m_ready;
    wire last_loaded = next_index == loaded;
    wire waiting_event = m_valid && !m_ready;
    wire can_replace = !m_valid || m_ready;
    wire keep_prefix = (cfg_keep != 8'd0) && ((cfg_keep & (cfg_keep + 1'b1)) == 8'd0);

    logic [3:0] result;
    wire append = req && cfg_op == `RP_APPEND && result == `RP_OK;
    wire clear = req && cfg_op == `RP_CLEAR && result == `RP_OK;
    wire start = req && cfg_op == `RP_START && result == `RP_OK;
    wire pause_cmd = req && cfg_op == `RP_PAUSE && result == `RP_OK;
    wire resume_cmd = req && cfg_op == `RP_RESUME && result == `RP_OK;
    wire stop = req && cfg_op == `RP_STOP && result == `RP_OK;
    wire run_edge = active && !paused && !pause_cmd && !stop && !stop_pending;

    wire [31:0] event_tick;
    wire [63:0] event_data;
    wire [7:0] event_keep;
    wire event_last;
    wire [15:0] event_tag;
    wire [EVENT_W-1:0] write_word;
    wire [EVENT_W-1:0] read_word;
    wire [47:0] due = sat_add_tick(pass_base, 48'(event_tick));
    wire final_pass = pass_number == pass_limit;
    wire end_pass = deliver && last_loaded;
    wire offer = run_edge && can_replace && !last_loaded && due <= now_tick;

    // Add a delay to the epoch clock. Saturation keeps time ordered at the limit.
    function automatic logic [47:0] sat_add_tick(input logic [47:0] tick, amount);
        logic [48:0] sum;
        begin
            sum = {1'b0,tick} + {1'b0,amount};
            sat_add_tick = sum[48] ? '1 : sum[47:0];
        end
    endfunction

    // Accumulate performance statistics without wrapping after a long run.
    function automatic logic [31:0] sat_inc(input logic [31:0] value);
        sat_inc = (&value) ? value : value + 1'b1;
    endfunction

    // Return zero for unused lanes so stale memory bytes cannot escape on replay.
    function automatic logic [63:0] mask_word(input logic [63:0] data, input logic [7:0] keep);
        logic [63:0] clean;
        begin
            for (integer lane = 0; lane < 8; lane++)
                clean[8*lane+:8] = keep[lane] ? data[8*lane+:8] : 8'd0;
            mask_word = clean;
        end
    endfunction

    assign cfg_ready = !rst && (!rsp_valid || rsp_ready);
    assign write_word = {cfg_tick, mask_word(cfg_data,cfg_keep), cfg_keep, cfg_last, cfg_tag};
    assign {event_tick,event_data,event_keep,event_last,event_tag} = read_word;

    replay_event_store #(.DEPTH(DEPTH),.INDEX_W(INDEX_W),.WORD_W(EVENT_W)) events (
        .clk, .write_enable(append), .write_index(INDEX_W'(loaded)), .write_word,
        .read_index(INDEX_W'(next_index)), .read_word
    );

    // Invalid commands only update the response slot; they leave the tape intact.
    always_comb begin
        result = `RP_OK;
        case (cfg_op)
            `RP_APPEND: begin
                if (active)
                    result = `RP_BUSY;
                else if (loaded == COUNT_W'(DEPTH))
                    result = `RP_FULL;
                else if (loaded != 0 && cfg_tick < latest)
                    result = `RP_BAD_TIME;
                else if (!keep_prefix || (!cfg_last && cfg_keep != 8'hff))
                    result = `RP_BAD_KEEP;
                else if (image_open && cfg_tag != image_tag)
                    result = `RP_BAD_TAG;
            end
            `RP_CLEAR: if (active) result = `RP_BUSY;
            `RP_START: begin
                if (active)
                    result = `RP_BUSY;
                else if (loaded == 0)
                    result = `RP_EMPTY;
                else if (image_open)
                    result = `RP_OPEN_PACKET;
                else if (cfg_repeats == 0)
                    result = `RP_BAD_REPEAT;
            end
            `RP_PAUSE: if (!active || paused || stop_pending) result = `RP_BAD_STATE;
            `RP_RESUME: if (!active || !paused || stop_pending) result = `RP_BAD_STATE;
            `RP_STOP: if (!active || stop_pending) result = `RP_BAD_STATE;
            default: result = `RP_BAD_OP;
        endcase
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            loaded <= 0;
            latest <= 0;
            image_open <= 0;
            image_tag <= 0;
            rsp_valid <= 0;
            rsp_status <= 0;
            rsp_length <= 0;
        end else if (req) begin
            rsp_valid <= 1;
            rsp_status <= result;
            rsp_length <= clear ? '0 : (append ? loaded + 1'b1 : loaded);
            if (clear) begin
                loaded <= 0;
                latest <= 0;
                image_open <= 0;
            end else if (append) begin
                loaded <= loaded + 1'b1;
                latest <= cfg_tick;
                image_open <= !cfg_last;
                image_tag <= cfg_tag;
            end
        end else if (rsp_valid && rsp_ready) begin
            rsp_valid <= 0;
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            active <= 0;
            paused <= 0;
            done <= 0;
            now_tick <= 0;
            pass_number <= 0;
            pass_limit <= 0;
            repeat_gap <= 0;
            pass_base <= 0;
            next_index <= 0;
            stop_pending <= 0;
            m_valid <= 0;
            m_data <= 0;
            m_keep <= 0;
            m_last <= 0;
            m_tag <= 0;
            m_due <= 0;
            m_offered <= 0;
            m_pass <= 0;
            emitted_beats <= 0;
            emitted_packets <= 0;
            late_beats <= 0;
            completed_passes <= 0;
        end else begin
            done <= 0;
            if (start) begin
                active <= 1;
                paused <= 0;
                stop_pending <= 0;
                now_tick <= 0;
                pass_number <= 1;
                pass_limit <= cfg_repeats;
                repeat_gap <= cfg_gap;
                pass_base <= 0;
                next_index <= 0;
                emitted_beats <= 0;
                emitted_packets <= 0;
                late_beats <= 0;
                completed_passes <= 0;
            end else if (active) begin
                if (pause_cmd) paused <= 1;
                if (resume_cmd) paused <= 0;
                if (run_edge) now_tick <= sat_add_tick(now_tick, 48'd1);
                if (deliver) begin
                    m_valid <= 0;
                    emitted_beats <= sat_inc(emitted_beats);
                    if (m_last)
                        emitted_packets <= sat_inc(emitted_packets);
                    if (m_offered > m_due)
                        late_beats <= sat_inc(late_beats);
                    if (end_pass)
                        completed_passes <= sat_inc(completed_passes);
                end
                if ((stop || stop_pending) && !waiting_event) begin
                    active <= 0;
                    paused <= 0;
                    stop_pending <= 0;
                    done <= 1;
                end else if (stop) begin
                    stop_pending <= 1;
                end else if (end_pass) begin
                    if (final_pass) begin
                        active <= 0;
                        paused <= 0;
                        done <= 1;
                    end else begin
                        pass_number <= pass_number + 1'b1;
                        pass_base <= sat_add_tick(now_tick, 48'(repeat_gap) + 48'd1);
                        next_index <= 0;
                    end
                end else if (offer) begin
                    m_valid <= 1;
                    m_data <= event_data;
                    m_keep <= event_keep;
                    m_last <= event_last;
                    m_tag <= event_tag;
                    m_due <= due;
                    m_offered <= now_tick;
                    m_pass <= pass_number;
                    next_index <= next_index + 1'b1;
                end
            end
        end
    end
endmodule

`default_nettype wire
