`timescale 1ns/1ps
`default_nettype none

module dma_interrupts (
    input  wire          clk,
    input  wire          reset,
    input  wire          clear,
    input  wire          completion,
    input  wire          force_notification,
    input  wire          acknowledge,
    input  wire [7:0]    minimum_batch,
    input  wire [31:0]   maximum_delay,
    output logic         request,
    output logic [7:0]   unacknowledged
);
    logic [31:0] elapsed;
    wire [7:0] before_completion = acknowledge ? 8'd0 : unacknowledged;
    wire [31:0] elapsed_with_ack = acknowledge ? 32'd0 : elapsed;
    wire [7:0] pending_next =
        completion && before_completion != '1 ? before_completion + 1'b1 : before_completion;
    wire [31:0] delay_next =
        pending_next != 0 && elapsed_with_ack != '1 ? elapsed_with_ack + 1'b1 : elapsed_with_ack;
    wire timer_expired = maximum_delay != 0 && delay_next >= maximum_delay;
    wire batch_full = minimum_batch != 0 && pending_next >= minimum_batch;

    always_ff @(posedge clk) begin
        if (reset || clear) begin
            request <= 1'b0;
            unacknowledged <= '0;
            elapsed <= '0;
        end else begin
            request <= (request && !acknowledge) ||
                       (completion && force_notification) ||
                       (pending_next != 0 && (timer_expired || batch_full));
            unacknowledged <= pending_next;
            elapsed <= pending_next != 0 ? delay_next : 32'd0;
        end
    end
endmodule

`default_nettype wire
