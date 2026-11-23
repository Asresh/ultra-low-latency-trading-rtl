`timescale 1ns/1ps
`default_nettype none

// Keep the latest DEPTH observations before a selected fault. The trigger
// record remains in the ring even when post_count equals DEPTH-1.
module latency_trace #(
    parameter integer ADDRESS_W = 4,
    parameter integer DATA_W = 320,
    localparam integer DEPTH = 1 << ADDRESS_W,
    localparam integer COUNT_W = ADDRESS_W + 1
) (
    input  wire                clk,
    input  wire                reset,
    input  wire                arm,
    input  wire                enable,
    input  wire [7:0]          trigger_mask,
    input  wire [63:0]         latency_threshold,
    input  wire [ADDRESS_W-1:0] post_count,
    input  wire                sample_valid,
    input  wire [2:0]          sample_status,
    input  wire [63:0]         sample_latency,
    input  wire [63:0]         sample_tick,
    input  wire [DATA_W-1:0]  sample_data,
    output wire                armed,
    output wire                frozen,
    output logic [31:0]        traces,
    output logic               m_valid,
    input  wire                m_ready,
    output logic [DATA_W-1:0] m_data,
    output logic               m_first,
    output logic               m_last
);
    localparam logic [2:0] IDLE = 0, CAPTURE = 1, AFTER_TRIGGER = 2, HEADER = 3, DRAIN = 4;
    logic [2:0] state;
    logic [DATA_W-1:0] records [0:DEPTH-1];
    logic [ADDRESS_W-1:0] writer, reader, trigger_slot;
    logic [ADDRESS_W-1:0] post_remaining;
    logic [COUNT_W-1:0] captured;
    logic [COUNT_W-1:0] remaining;
    logic [63:0] trigger_tick;
    logic [2:0] trigger_status;
    logic [31:0] trace_number;
    logic threshold_trigger;

    wire time_trigger =
        latency_threshold != 0 && sample_status == 3'd1 && sample_latency >= latency_threshold;
    wire trigger_hit = trigger_mask[sample_status] || time_trigger;
    wire arming = arm && !frozen;
    wire capture_edge = sample_valid && armed && !arming;
    wire [COUNT_W-1:0] captured_next =
        captured == COUNT_W'(DEPTH) ? captured : captured + 1'b1;
    wire [ADDRESS_W-1:0] following_write = writer + 1'b1;
    wire [ADDRESS_W-1:0] oldest_next = following_write - ADDRESS_W'(captured_next);
    wire [ADDRESS_W-1:0] offset = trigger_slot - reader;
    wire fire = m_valid && m_ready;

    assign armed = state == CAPTURE || state == AFTER_TRIGGER;
    assign frozen = state == HEADER || state == DRAIN;

    always_comb begin
        m_data = '0;
        m_valid = !reset && frozen;
        m_first = m_valid && state == HEADER;
        m_last = m_valid && state == DRAIN && remaining == 1;
        if (state == HEADER) begin
            m_data[159:128] = 32'h4c54_5231;
            m_data[127:96] = trace_number;
            m_data[95:32] = trigger_tick;
            m_data[31:24] = 8'(captured);
            m_data[23:16] = 8'(offset);
            m_data[15:13] = trigger_status;
            m_data[12] = threshold_trigger;
        end else if (state == DRAIN) begin
            m_data = records[reader];
        end
    end

    always_ff @(posedge clk) begin
        if (capture_edge && !reset && enable)
            records[writer] <= sample_data;
    end

    always_ff @(posedge clk) begin
        if (reset) begin
            state <= IDLE;
            writer <= '0;
            reader <= '0;
            captured <= '0;
            remaining <= '0;
            post_remaining <= '0;
            trigger_slot <= '0;
            trigger_tick <= '0;
            trigger_status <= '0;
            trace_number <= '0;
            traces <= '0;
            threshold_trigger <= 1'b0;
        end else begin
            if (arming) begin
                writer <= '0;
                captured <= '0;
                state <= CAPTURE;
            end else if (armed && !enable) begin
                state <= IDLE;
            end else if (capture_edge && enable) begin
                writer <= following_write;
                captured <= captured_next;
                if (state == CAPTURE && trigger_hit) begin
                    trigger_slot <= writer;
                    trigger_tick <= sample_tick;
                    trigger_status <= sample_status;
                    threshold_trigger <= time_trigger;
                    post_remaining <= post_count;
                    if (post_count == 0) begin
                        reader <= oldest_next;
                        remaining <= captured_next;
                        state <= HEADER;
                    end else begin
                        state <= AFTER_TRIGGER;
                    end
                end else if (state == AFTER_TRIGGER) begin
                    post_remaining <= post_remaining - 1'b1;
                    if (post_remaining == 1) begin
                        reader <= oldest_next;
                        remaining <= captured_next;
                        state <= HEADER;
                    end
                end
            end else if (fire) begin
                if (state == HEADER) begin
                    state <= DRAIN;
                end else if (remaining == 1) begin
                    remaining <= '0;
                    state <= IDLE;
                    trace_number <= trace_number + 1'b1;
                    if (traces != '1)
                        traces <= traces + 1'b1;
                end else begin
                    remaining <= remaining - 1'b1;
                    reader <= reader + 1'b1;
                end
            end
        end
    end
endmodule

`default_nettype wire
