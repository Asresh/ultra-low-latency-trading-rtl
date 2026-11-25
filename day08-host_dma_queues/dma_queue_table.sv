`timescale 1ns/1ps
`default_nettype none

module dma_queue_table #(
    parameter integer QUEUES = 2,
    parameter integer RING_LOG = 3,
    parameter bit ENFORCE_QUEUE_ISOLATION = 1'b0,
    localparam integer Q_W = QUEUES > 1 ? $clog2(QUEUES) : 1,
    localparam integer DEPTH = 1 << RING_LOG
) (
    input  wire                    clk,
    input  wire                    reset,
    input  wire                    cfg_valid,
    output wire                    cfg_ready,
    input  wire [Q_W-1:0]          cfg_queue,
    input  wire                    cfg_enable,
    input  wire [63:0]             cfg_submission_base,
    input  wire [63:0]             cfg_completion_base,
    input  wire [63:0]             cfg_payload_low,
    input  wire [63:0]             cfg_payload_high,
    input  wire [31:0]             cfg_seed,
    input  wire [7:0]              cfg_irq_batch,
    input  wire [31:0]             cfg_irq_delay,
    output logic                    cfg_ack,
    output logic                    cfg_ok,
    input  wire                    host_valid,
    output wire                    host_ready,
    input  wire [Q_W-1:0]          host_queue,
    input  wire [31:0]             host_submit_tail,
    input  wire [31:0]             host_complete_head,
    output logic                    host_ack,
    output logic                    host_ok,
    input  wire                    active,
    input  wire [Q_W-1:0]          active_queue,
    input  wire                    retire,
    input  wire                    immediate_irq,
    input  wire [QUEUES-1:0]       irq_ack,
    output wire [QUEUES-1:0]       irq,
    output wire [QUEUES-1:0]       eligible,
    input  wire [Q_W-1:0]          selected,
    output wire [63:0]             selected_sq_addr,
    output wire [63:0]             selected_cq_addr,
    output wire [63:0]             selected_payload_low,
    output wire [63:0]             selected_payload_high,
    input  wire [Q_W-1:0]          inspect_queue,
    output wire                    inspect_enabled,
    output wire [31:0]             inspect_sq_head,
    output wire [31:0]             inspect_sq_tail,
    output wire [31:0]             inspect_cq_head,
    output wire [31:0]             inspect_cq_tail,
    output wire [7:0]              inspect_irq_backlog
);
    logic [QUEUES-1:0] enabled;
    logic [63:0] submission_base [0:QUEUES-1];
    logic [63:0] completion_base [0:QUEUES-1];
    logic [63:0] payload_low [0:QUEUES-1];
    logic [63:0] payload_high [0:QUEUES-1];
    logic [31:0] sq_head [0:QUEUES-1];
    logic [31:0] sq_tail [0:QUEUES-1];
    logic [31:0] cq_head [0:QUEUES-1];
    logic [31:0] cq_tail [0:QUEUES-1];
    logic [7:0] irq_batch [0:QUEUES-1];
    logic [31:0] irq_delay [0:QUEUES-1];
    wire [7:0] irq_backlogs [0:QUEUES-1];

    localparam logic [64:0] RING_BYTES = 65'(DEPTH) << 5;
    wire [64:0] submission_end = {1'b0, cfg_submission_base} + RING_BYTES;
    wire [64:0] completion_end = {1'b0, cfg_completion_base} + RING_BYTES;
    wire [31:0] submit_advance = host_submit_tail - sq_tail[host_queue];
    wire [31:0] complete_advance = host_complete_head - cq_head[host_queue];
    wire [31:0] sq_pending = sq_tail[host_queue] - sq_head[host_queue];
    wire [31:0] cq_pending = cq_tail[host_queue] - cq_head[host_queue];
    wire can_replace = !enabled[cfg_queue] ||
        (sq_head[cfg_queue] == sq_tail[cfg_queue] && cq_head[cfg_queue] == cq_tail[cfg_queue]);
    wire layout_ok = !submission_end[64] && !completion_end[64] &&
        cfg_submission_base[4:0] == 0 && cfg_completion_base[4:0] == 0 &&
        cfg_payload_low[2:0] == 0 && cfg_payload_high[2:0] == 0 &&
        cfg_payload_low < cfg_payload_high &&
        (submission_end <= {1'b0, cfg_completion_base} ||
         completion_end <= {1'b0, cfg_submission_base}) &&
        (submission_end <= {1'b0, cfg_payload_low} ||
         {1'b0, cfg_payload_high} <= {1'b0, cfg_submission_base}) &&
        (completion_end <= {1'b0, cfg_payload_low} ||
         {1'b0, cfg_payload_high} <= {1'b0, cfg_completion_base});

    function automatic logic overlaps(
        input logic [64:0] a_start, a_end, b_start, b_end
    );
        overlaps = a_start < b_end && b_start < a_end;
    endfunction

    wire isolated;
    if (ENFORCE_QUEUE_ISOLATION && QUEUES > 1) begin : g_isolation
        wire [QUEUES-1:0] conflicts;
        for (genvar q = 0; q < QUEUES; q++) begin : peer
            wire [64:0] starts [0:2], ends [0:2];
            wire [8:0] intersections;
            assign starts[0] = {1'b0, submission_base[q]};
            assign starts[1] = {1'b0, completion_base[q]};
            assign starts[2] = {1'b0, payload_low[q]};
            assign ends[0] = starts[0] + RING_BYTES;
            assign ends[1] = starts[1] + RING_BYTES;
            assign ends[2] = {1'b0, payload_high[q]};
            for (genvar region = 0; region < 3; region++) begin : compare_region
                assign intersections[3*region] = overlaps(
                    {1'b0, cfg_submission_base}, submission_end, starts[region], ends[region]
                );
                assign intersections[3*region+1] = overlaps(
                    {1'b0, cfg_completion_base}, completion_end, starts[region], ends[region]
                );
                assign intersections[3*region+2] = overlaps(
                    {1'b0, cfg_payload_low}, {1'b0, cfg_payload_high}, starts[region], ends[region]
                );
            end
            assign conflicts[q] = enabled[q] && cfg_queue != Q_W'(q) && (|intersections);
        end
        assign isolated = !(|conflicts);
    end else begin : g_legacy_layout
        assign isolated = 1'b1;
    end
    wire configure = cfg_valid && cfg_ready &&
                     (({1'b0, cfg_queue} < (Q_W+1)'(QUEUES)) && can_replace &&
                      (!cfg_enable || (layout_ok && isolated)));
    wire host_update = host_valid && host_ready &&
                       {1'b0, host_queue} < (Q_W+1)'(QUEUES) && enabled[host_queue] &&
                       sq_pending <= DEPTH && submit_advance <= DEPTH - sq_pending &&
                       complete_advance <= cq_pending;

    assign cfg_ready = !reset && !active;
    assign host_ready = !reset && !(cfg_valid && cfg_ready);
    assign inspect_enabled = {1'b0, inspect_queue} < (Q_W+1)'(QUEUES) && enabled[inspect_queue];
    assign inspect_sq_head = inspect_enabled ? sq_head[inspect_queue] : '0;
    assign inspect_sq_tail = inspect_enabled ? sq_tail[inspect_queue] : '0;
    assign inspect_cq_head = inspect_enabled ? cq_head[inspect_queue] : '0;
    assign inspect_cq_tail = inspect_enabled ? cq_tail[inspect_queue] : '0;
    assign inspect_irq_backlog = inspect_enabled ? irq_backlogs[inspect_queue] : '0;
    assign selected_sq_addr = submission_base[selected] + (64'(sq_head[selected][RING_LOG-1:0]) << 5);
    assign selected_cq_addr = completion_base[selected] + (64'(cq_tail[selected][RING_LOG-1:0]) << 5);
    assign selected_payload_low = payload_low[selected];
    assign selected_payload_high = payload_high[selected];

    for (genvar q = 0; q < QUEUES; q++) begin : g_irq
        assign eligible[q] = enabled[q] && sq_tail[q] != sq_head[q] &&
                             cq_tail[q] - cq_head[q] < DEPTH;
        dma_interrupts irq_control (
            .clk, .reset, .clear(configure && cfg_queue == Q_W'(q)),
            .completion(retire && active_queue == Q_W'(q)),
            .force_notification(immediate_irq), .acknowledge(irq_ack[q]),
            .minimum_batch(irq_batch[q]), .maximum_delay(irq_delay[q]),
            .request(irq[q]), .unacknowledged(irq_backlogs[q])
        );
    end

    always_ff @(posedge clk) begin
        if (reset) begin
            cfg_ack <= 1'b0;
            cfg_ok <= 1'b0;
            host_ack <= 1'b0;
            host_ok <= 1'b0;
            enabled <= '0;
            for (integer q = 0; q < QUEUES; q++) begin
                sq_head[q] <= '0;
                sq_tail[q] <= '0;
                cq_head[q] <= '0;
                cq_tail[q] <= '0;
                submission_base[q] <= '0;
                completion_base[q] <= '0;
                payload_low[q] <= '0;
                payload_high[q] <= '0;
                irq_batch[q] <= '0;
                irq_delay[q] <= '0;
            end
        end else begin
            cfg_ack <= cfg_valid && cfg_ready;
            cfg_ok <= configure;
            host_ack <= host_valid && host_ready;
            host_ok <= host_update;
            if (configure) begin
                enabled[cfg_queue] <= cfg_enable;
                submission_base[cfg_queue] <= cfg_submission_base;
                completion_base[cfg_queue] <= cfg_completion_base;
                payload_low[cfg_queue] <= cfg_payload_low;
                payload_high[cfg_queue] <= cfg_payload_high;
                sq_head[cfg_queue] <= cfg_seed;
                sq_tail[cfg_queue] <= cfg_seed;
                cq_head[cfg_queue] <= cfg_seed;
                cq_tail[cfg_queue] <= cfg_seed;
                irq_batch[cfg_queue] <= cfg_irq_batch;
                irq_delay[cfg_queue] <= cfg_irq_delay;
            end
            if (host_update) begin
                sq_tail[host_queue] <= host_submit_tail;
                cq_head[host_queue] <= host_complete_head;
            end
            if (retire) begin
                sq_head[active_queue] <= sq_head[active_queue] + 1'b1;
                cq_tail[active_queue] <= cq_tail[active_queue] + 1'b1;
            end
        end
    end
endmodule

`default_nettype wire
