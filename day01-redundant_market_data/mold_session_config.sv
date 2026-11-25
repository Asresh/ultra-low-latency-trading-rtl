`timescale 1ns/1ps
`default_nettype none

// Commands compare the caller's epoch with the installed epoch. A successful
// command fences new work immediately and commits after the old leases/offers.
module mold_session_config #(
    parameter integer SLOTS = 3,
    parameter integer EPOCH_W = 16,
    parameter integer WAIT_W = 16,
    localparam integer SLOT_W = SLOTS > 1 ? $clog2(SLOTS) : 1,
    localparam integer KEY_W = $clog2(SLOTS + 1)
) (
    input  wire                       clk,
    input  wire                       rst,
    input  wire                       cfg_valid,
    output wire                       cfg_ready,
    input  wire [KEY_W-1:0]           cfg_slot,
    input  wire [EPOCH_W-1:0]         cfg_expected_epoch,
    input  wire                       cfg_enable,
    input  wire                       cfg_force,
    input  wire [79:0]                cfg_session,
    input  wire [63:0]                cfg_sequence,
    input  wire [WAIT_W-1:0]          cfg_request_spacing,
    output logic                      ack_valid,
    input  wire                       ack_ready,
    output logic [1:0]                ack_status,
    output logic [KEY_W-1:0]          ack_slot,
    output logic [EPOCH_W-1:0]        ack_epoch,
    output logic                      ack_active,
    output logic [1:0]                ack_cut_lanes,
    input  wire [SLOTS-1:0]           leased,
    input  wire [SLOTS-1:0]           output_pending,
    input  wire [SLOTS-1:0]           install_ready,
    output logic [SLOTS-1:0]          active,
    output wire [SLOTS*EPOCH_W-1:0]   epochs,
    output wire [SLOTS*WAIT_W-1:0]    request_spacing,
    output logic [SLOTS-1:0]          fenced,
    output logic [SLOTS-1:0]          install,
    output logic [79:0]               install_session,
    output logic [63:0]               install_sequence,
    output wire                       cut_valid,
    output wire [SLOT_W-1:0]          cut_slot,
    input  wire [1:0]                 cut_lanes
);
    localparam logic [1:0] OK = 0, BAD_SLOT = 1, STALE_EPOCH = 2, EXHAUSTED = 3;
    logic [EPOCH_W-1:0] epoch [0:SLOTS-1];
    logic [WAIT_W-1:0] spacing [0:SLOTS-1];
    logic pending;
    logic [SLOT_W-1:0] pending_slot;
    logic pending_enable;
    logic [WAIT_W-1:0] pending_spacing;
    logic [1:0] pending_cut_lanes;
    wire valid_slot = int'(cfg_slot) < SLOTS;
    wire [SLOT_W-1:0] slot_index = valid_slot ? SLOT_W'(cfg_slot) : '0;
    wire commit = pending && !leased[pending_slot] && !output_pending[pending_slot] &&
                  install_ready[pending_slot];
    wire fire = cfg_valid && cfg_ready;
    wire current_epoch = epoch[slot_index] == cfg_expected_epoch;
    wire can_install = valid_slot && current_epoch && epoch[slot_index] != '1;
    wire start = fire && can_install;

    assign cfg_ready = !rst && !pending && (!ack_valid || ack_ready);
    assign cut_valid = start && cfg_force;
    assign cut_slot = slot_index;
    for (genvar slot = 0; slot < SLOTS; slot++) begin : flatten_state
        assign epochs[slot*EPOCH_W+:EPOCH_W] = epoch[slot];
        assign request_spacing[slot*WAIT_W+:WAIT_W] = spacing[slot];
    end

    always_comb begin
        fenced = '0;
        install = '0;
        if (pending)
            fenced[pending_slot] = 1'b1;
        if (start)
            fenced[slot_index] = 1'b1;
        if (commit && !rst)
            install[pending_slot] = 1'b1;
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            pending <= 1'b0;
            pending_slot <= '0;
            pending_enable <= 1'b0;
            pending_spacing <= '0;
            pending_cut_lanes <= '0;
            install_session <= '0;
            install_sequence <= '0;
            active <= '0;
            ack_valid <= 1'b0;
            ack_status <= OK;
            ack_slot <= '0;
            ack_epoch <= '0;
            ack_active <= 1'b0;
            ack_cut_lanes <= '0;
            for (integer index = 0; index < SLOTS; index++) begin
                epoch[index] <= '0;
                spacing[index] <= '0;
            end
        end else begin
            if (ack_valid && ack_ready)
                ack_valid <= 1'b0;
            if (start) begin
                pending <= 1'b1;
                pending_slot <= slot_index;
                pending_enable <= cfg_enable;
                pending_spacing <= cfg_request_spacing;
                pending_cut_lanes <= cfg_force ? cut_lanes : '0;
                install_session <= cfg_session;
                install_sequence <= cfg_sequence;
            end else if (fire) begin
                ack_valid <= 1'b1;
                ack_status <= !valid_slot ? BAD_SLOT :
                              !current_epoch ? STALE_EPOCH : EXHAUSTED;
                ack_slot <= cfg_slot;
                ack_epoch <= valid_slot ? epoch[slot_index] : '0;
                ack_active <= valid_slot && active[slot_index];
                ack_cut_lanes <= '0;
            end
            if (commit) begin
                pending <= 1'b0;
                epoch[pending_slot] <= epoch[pending_slot] + 1'b1;
                spacing[pending_slot] <= pending_spacing;
                active[pending_slot] <= pending_enable;
                ack_valid <= 1'b1;
                ack_status <= OK;
                ack_slot <= KEY_W'(pending_slot);
                ack_epoch <= epoch[pending_slot] + 1'b1;
                ack_active <= pending_enable;
                ack_cut_lanes <= pending_cut_lanes;
            end
        end
    end
endmodule

`default_nettype wire
