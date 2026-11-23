`timescale 1ns/1ps
`default_nettype none

// Elastic dual-clock queue. Both local resets must be asserted together.
// There is a separately registered read skid slot, in addition to RAM depth.
module latency_async_fifo #(
    parameter integer WIDTH = 84,
    parameter integer ADDRESS_W = 3,
    localparam integer DEPTH = 1 << ADDRESS_W,
    localparam integer POINTER_W = ADDRESS_W + 1
) (
    input  wire               wclk,
    input  wire               wreset,
    input  wire               s_valid,
    output wire               s_ready,
    input  wire [WIDTH-1:0]   s_data,
    input  wire               rclk,
    input  wire               rreset,
    output wire               m_valid,
    input  wire               m_ready,
    output wire [WIDTH-1:0]   m_data
);
    logic [WIDTH-1:0] memory [0:DEPTH-1];
    logic [POINTER_W-1:0] wbin, rbin;
    logic [POINTER_W-1:0] wgray, rgray;
    (* ASYNC_REG = "TRUE" *) logic [POINTER_W-1:0] rq1_wgray, rq2_wgray;
    (* ASYNC_REG = "TRUE" *) logic [POINTER_W-1:0] wq1_rgray, wq2_rgray;
    (* ASYNC_REG = "TRUE" *) logic [1:0] wq_rup, rq_wup;
    logic full;
    logic buffered;
    logic [WIDTH-1:0] read_data;

    wire write_link = !wreset && wq_rup[1];
    wire read_link = !rreset && rq_wup[1];
    wire push = s_valid && s_ready;
    wire memory_empty = rgray == rq2_wgray;
    wire refill = read_link && !memory_empty && (!buffered || m_ready);
    wire [POINTER_W-1:0] wbin_next = wbin + POINTER_W'(push);
    wire [POINTER_W-1:0] rbin_next = rbin + POINTER_W'(refill);
    wire [POINTER_W-1:0] wgray_next = (wbin_next >> 1) ^ wbin_next;
    wire [POINTER_W-1:0] rgray_next = (rbin_next >> 1) ^ rbin_next;
    localparam logic [POINTER_W-1:0] FULL_MASK =
        POINTER_W'(3) << (POINTER_W - 2);

    assign s_ready = write_link && !full;
    assign m_valid = read_link && buffered;
    assign m_data = read_data;

    always_ff @(posedge wclk) begin
        if (push)
            memory[wbin[ADDRESS_W-1:0]] <= s_data;
    end

    always_ff @(posedge rclk) begin
        if (refill)
            read_data <= memory[rbin[ADDRESS_W-1:0]];
    end

    always_ff @(posedge wclk or posedge wreset) begin
        if (wreset) begin
            wbin <= '0;
            wgray <= '0;
            wq1_rgray <= '0;
            wq2_rgray <= '0;
            wq_rup <= '0;
            full <= 1'b0;
        end else begin
            wq1_rgray <= rgray;
            wq2_rgray <= wq1_rgray;
            wq_rup <= {wq_rup[0], !rreset};
            wbin <= wbin_next;
            wgray <= wgray_next;
            full <= wgray_next == (wq2_rgray ^ FULL_MASK);
        end
    end

    always_ff @(posedge rclk or posedge rreset) begin
        if (rreset) begin
            rbin <= '0;
            rgray <= '0;
            rq1_wgray <= '0;
            rq2_wgray <= '0;
            rq_wup <= '0;
            buffered <= 1'b0;
        end else begin
            rq1_wgray <= wgray;
            rq2_wgray <= rq1_wgray;
            rq_wup <= {rq_wup[0], !wreset};
            rbin <= rbin_next;
            rgray <= rgray_next;
            if (refill)
                buffered <= 1'b1;
            else if (m_valid && m_ready)
                buffered <= 1'b0;
        end
    end
endmodule

`default_nettype wire
