`timescale 1ns/1ps
`default_nettype none

// Attach a single-use refund handle to every transferred resource allocation.
module leased_credit_scheduler #(
    parameter integer CLASSES = 3,
    parameter integer DEPTH = 4,
    parameter integer CREDIT_W = 8,
    parameter integer TAG_W = 16,
    parameter integer PAYLOAD_W = 32,
    parameter integer LEASE_SLOTS = 8,
    parameter integer GEN_W = 48,
    localparam integer CLASS_W = CLASSES > 1 ? $clog2(CLASSES) : 1,
    localparam integer COUNT_W = $clog2(DEPTH+1),
    localparam integer SLOT_W = LEASE_SLOTS > 1 ? $clog2(LEASE_SLOTS) : 1,
    localparam integer LEASE_W = GEN_W + SLOT_W
) (
    input  wire                  clk,
    input  wire                  rst,
    input  wire                  s_valid,
    output wire                  s_ready,
    input  wire [CLASS_W-1:0]    s_class,
    input  wire [CREDIT_W-1:0]   s_cost,
    input  wire [TAG_W-1:0]      s_tag,
    input  wire [PAYLOAD_W-1:0]  s_payload,
    output wire                  s_rejected,

    output wire                  m_valid,
    input  wire                  m_ready,
    output wire [CLASS_W-1:0]    m_class,
    output wire [CREDIT_W-1:0]   m_cost,
    output wire [TAG_W-1:0]      m_tag,
    output wire [PAYLOAD_W-1:0]  m_payload,
    output wire [LEASE_W-1:0]    m_lease,

    input  wire                  ctl_valid,
    output wire                  ctl_ready,
    input  wire [1:0]            ctl_op,
    input  wire [CLASS_W-1:0]    ctl_class,
    input  wire [CREDIT_W-1:0]   ctl_amount,
    input  wire [3:0]            ctl_weight,
    input  wire [LEASE_W-1:0]    ctl_lease,
    input  wire [TAG_W-1:0]      ctl_tag,

    output wire                  report_valid,
    input  wire                  report_ready,
    output wire [1:0]            report_op,
    output wire [CLASS_W-1:0]    report_class,
    output wire [2:0]            report_status,
    output wire [CREDIT_W-1:0]   report_capacity,
    output wire [CREDIT_W-1:0]   report_available,
    output wire [CREDIT_W-1:0]   report_inflight,
    output wire [COUNT_W-1:0]    report_queued,
    output wire [3:0]            report_weight,
    output logic [LEASE_W-1:0]   report_lease,
    output logic [TAG_W-1:0]     report_tag,
    output wire [LEASE_SLOTS-1:0] lease_active,
    output wire [LEASE_SLOTS-1:0] lease_exhausted
);
    localparam logic [1:0] RETURN_LEASE = 1;
    localparam logic [2:0] OK = 0, BAD_SLOT = 5, STALE_LEASE = 6;
    logic [LEASE_SLOTS-1:0] active;
    logic [GEN_W-1:0] generation [0:LEASE_SLOTS-1];
    logic [CLASS_W-1:0] classes [0:LEASE_SLOTS-1];
    logic [CREDIT_W-1:0] costs [0:LEASE_SLOTS-1];

    logic slot_found, slot_held;
    logic [SLOT_W-1:0] free_slot, held_slot;
    wire [SLOT_W-1:0] offered_slot = slot_held ? held_slot : free_slot;
    wire [GEN_W-1:0] offered_generation = generation[offered_slot] + 1'b1;

    wire native_valid;
    wire native_ready = m_ready && (slot_found || slot_held);
    wire [CLASS_W-1:0] native_class;
    wire [CREDIT_W-1:0] native_cost;
    wire [TAG_W-1:0] native_tag;
    wire [PAYLOAD_W-1:0] native_payload;

    logic command_pending, local_report;
    logic [2:0] local_status;
    wire is_return = ctl_op == RETURN_LEASE;
    wire [SLOT_W-1:0] requested_slot = ctl_lease[SLOT_W-1:0];
    wire slot_in_range = int'(requested_slot) < LEASE_SLOTS;
    wire [SLOT_W-1:0] return_slot = slot_in_range ? requested_slot : '0;
    wire lease_matches = slot_in_range && active[return_slot] &&
                         ctl_lease[SLOT_W+:GEN_W] == generation[return_slot];
    wire reject_return = is_return && !lease_matches;
    wire native_ctl_ready, native_report_valid;
    wire [1:0] native_report_op;
    wire [CLASS_W-1:0] native_report_class;
    wire [2:0] native_status;
    wire [CREDIT_W-1:0] native_capacity, native_available, native_inflight;
    wire [COUNT_W-1:0] native_queued;
    wire [3:0] native_weight;
    wire apply_command = ctl_valid && ctl_ready;
    wire retire = report_valid && report_ready && !local_report &&
                  native_report_op == RETURN_LEASE && native_status == OK;

    assign m_valid = !rst && native_valid && (slot_found || slot_held);
    assign m_lease = m_valid ? {offered_generation,offered_slot} : '0;
    assign m_class = m_valid ? native_class : '0;
    assign m_cost = m_valid ? native_cost : '0;
    assign m_tag = m_valid ? native_tag : '0;
    assign m_payload = m_valid ? native_payload : '0;

    assign ctl_ready = !rst && !command_pending && (reject_return || native_ctl_ready);
    assign report_valid = !rst && command_pending && (local_report || native_report_valid);
    assign report_op = local_report ? RETURN_LEASE : native_report_op;
    assign report_class = local_report ? '0 : native_report_class;
    assign report_status = local_report ? local_status : native_status;
    assign report_capacity = local_report ? '0 : native_capacity;
    assign report_available = local_report ? '0 : native_available;
    assign report_inflight = local_report ? '0 : native_inflight;
    assign report_queued = local_report ? '0 : native_queued;
    assign report_weight = local_report ? '0 : native_weight;
    assign lease_active = active;

    for (genvar slot = 0; slot < LEASE_SLOTS; slot=slot+1) begin : g_status
        assign lease_exhausted[slot] = &generation[slot];
    end

    always_comb begin
        slot_found = 0;
        free_slot = '0;
        for (integer slot = 0; slot < LEASE_SLOTS; slot=slot+1) begin
            if (!slot_found && !active[slot] && !lease_exhausted[slot]) begin
                slot_found = 1;
                free_slot = SLOT_W'(slot);
            end
        end
    end

    credit_order_scheduler #(
        .CLASSES(CLASSES), .DEPTH(DEPTH), .CREDIT_W(CREDIT_W),
        .TAG_W(TAG_W), .PAYLOAD_W(PAYLOAD_W)
    ) scheduler (
        .clk, .rst, .s_valid, .s_ready, .s_class, .s_cost, .s_tag, .s_payload, .s_rejected,
        .m_valid(native_valid), .m_ready(native_ready), .m_class(native_class),
        .m_cost(native_cost), .m_tag(native_tag), .m_payload(native_payload),
        .ctl_valid(ctl_valid && !command_pending && !reject_return), .ctl_ready(native_ctl_ready),
        .ctl_op, .ctl_class(is_return ? classes[return_slot] : ctl_class),
        .ctl_amount(is_return ? costs[return_slot] : ctl_amount),
        .ctl_weight(is_return ? 4'd0 : ctl_weight),
        .report_valid(native_report_valid), .report_ready(command_pending && !local_report && report_ready),
        .report_op(native_report_op), .report_class(native_report_class), .report_status(native_status),
        .report_capacity(native_capacity), .report_available(native_available), .report_inflight(native_inflight),
        .report_queued(native_queued), .report_weight(native_weight)
    );

    always_ff @(posedge clk) begin
        if (rst) begin
            active <= '0;
            slot_held <= 0;
            held_slot <= '0;
            command_pending <= 0;
            local_report <= 0;
            local_status <= OK;
            report_lease <= '0;
            report_tag <= '0;
            for (integer slot = 0; slot < LEASE_SLOTS; slot=slot+1)
                generation[slot] <= '0;
        end else begin
            if (m_valid && !m_ready) begin
                slot_held <= 1;
                held_slot <= offered_slot;
            end
            if (m_valid && m_ready) begin
                slot_held <= 0;
                active[offered_slot] <= 1;
                generation[offered_slot] <= offered_generation;
                classes[offered_slot] <= native_class;
                costs[offered_slot] <= native_cost;
            end
            if (report_valid && report_ready)
                command_pending <= 0;
            if (apply_command) begin
                command_pending <= 1;
                local_report <= reject_return;
                local_status <= slot_in_range ? STALE_LEASE : BAD_SLOT;
                report_lease <= is_return ? ctl_lease : LEASE_W'(0);
                report_tag <= ctl_tag;
            end
            if (retire)
                active[report_lease[SLOT_W-1:0]] <= 0;
        end
    end
`ifdef FORMAL_LEASES
    logic past_valid = 0;
    always_ff @(posedge clk) begin
        past_valid <= 1;
        if (!past_valid)
            assume (rst);
        if (past_valid && !rst) begin
            if (!$past(rst) && $past(m_valid && !m_ready)) begin
                assert (m_valid);
                assert ($stable({m_lease,m_class,m_cost,m_tag,m_payload}));
            end
            if (!$past(rst) && $past(report_valid && !report_ready)) begin
                assert (report_valid);
                assert ($stable({report_tag,report_lease,report_op,report_status,report_class,
                                 report_capacity,report_available,report_inflight,
                                 report_queued,report_weight}));
            end
            if (m_valid) begin
                assert (int'(offered_slot) < LEASE_SLOTS);
                assert (!active[offered_slot] && !lease_exhausted[offered_slot]);
                assert (offered_generation != 0);
            end
            if (report_valid && !local_report && report_op == RETURN_LEASE) begin
                assert (report_status == OK);
                assert (active[report_lease[SLOT_W-1:0]]);
            end
            if (retire && m_valid && m_ready)
                assert (offered_slot != report_lease[SLOT_W-1:0]);
            cover (m_valid && m_ready);
            cover (retire);
            cover (report_valid && report_status == STALE_LEASE);
        end
    end
`endif

endmodule

`default_nettype wire
