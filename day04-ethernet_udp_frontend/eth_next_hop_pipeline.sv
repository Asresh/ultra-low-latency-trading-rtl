`timescale 1ns/1ps
`default_nettype none

// Route multiple independent requests while a neighbor cache completes
// lookups in any order. Only table slot IDs cross internal interfaces.
module eth_next_hop_pipeline #(
    parameter integer SLOTS = 8,
    parameter integer ROWS = 8,
    parameter integer TAG_W = 32,
    parameter integer EPOCH_W = 16,
    localparam integer SLOT_W = SLOTS > 1 ? $clog2(SLOTS) : 1,
    localparam integer ROW_W = $clog2(ROWS+1),
    localparam integer COUNT_W = $clog2(SLOTS+1)
) (
    input  wire                clk,
    input  wire                rst,
    input  wire [63:0]         clock_tick,
    input  wire                s_valid,
    output wire                s_ready,
    input  wire [TAG_W-1:0]    s_tag,
    input  wire                s_tagged,
    input  wire [11:0]         s_vid,
    input  wire [31:0]         s_destination,
    input  wire                s_allow_multicast,

    output wire                m_valid,
    input  wire                m_ready,
    output wire [TAG_W-1:0]    m_tag,
    output wire                m_tagged,
    output wire [11:0]         m_vid,
    output wire [31:0]         m_destination,
    output wire [31:0]         m_next_hop,
    output wire [47:0]         m_mac,
    output wire [3:0]          m_status,
    output wire [ROW_W-1:0]    m_row,
    output wire [EPOCH_W-1:0]  m_epoch,
    output wire [63:0]         m_start_tick,
    output wire [63:0]         m_finish_tick,
    output wire [COUNT_W-1:0]  occupied,
    output logic [31:0]        unexpected_neighbor_replies,
    output logic [31:0]        successful_lookups,
    output logic [31:0]        failed_lookups,

    output wire                arp_request_valid,
    input  wire                arp_request_ready,
    output wire                arp_request_tagged,
    output wire [11:0]         arp_request_vid,
    output wire [31:0]         arp_request_ip,
    output wire [SLOT_W-1:0]   arp_request_slot,
    input  wire                arp_result_valid,
    output wire                arp_result_ready,
    input  wire [2:0]          arp_result_status,
    input  wire [SLOT_W-1:0]   arp_result_slot,
    input  wire [47:0]         arp_result_mac,

    input  wire                cfg_valid,
    output wire                cfg_ready,
    input  wire [ROW_W-1:0]    cfg_row,
    input  wire                cfg_enabled,
    input  wire                cfg_tagged,
    input  wire [11:0]         cfg_vid,
    input  wire [31:0]         cfg_network,
    input  wire [5:0]          cfg_prefix,
    input  wire                cfg_reject,
    input  wire [31:0]         cfg_gateway,
    output wire                cfg_error,
    input  wire                commit_valid,
    output wire                commit_ready,
    output wire [EPOCH_W-1:0]  active_epoch
);
    localparam logic [1:0] FREE = 0, ROUTE = 1, WAIT_NEIGHBOR = 2, COMPLETE = 3;
    localparam logic [3:0] OK = 0, BAD_DESTINATION = 3, ARP_TIMEOUT = 4,
                           ARP_BUSY = 5, ARP_BAD_REPLY = 6, MULTICAST_DENIED = 7;
    logic [1:0] state [0:SLOTS-1];
    logic [TAG_W-1:0] tag [0:SLOTS-1];
    logic [12:0] vlan_scope [0:SLOTS-1];
    logic [31:0] destination [0:SLOTS-1];
    logic [31:0] next_hop [0:SLOTS-1];
    logic [47:0] link_address [0:SLOTS-1];
    logic [3:0] status [0:SLOTS-1];
    logic [ROW_W-1:0] route_row [0:SLOTS-1];
    logic [EPOCH_W-1:0] route_epoch [0:SLOTS-1];
    logic [63:0] start_tick [0:SLOTS-1], finish_tick [0:SLOTS-1];
    logic [SLOT_W-1:0] allocate_cursor, retire_cursor, held_at;
    logic held, free_found, complete_found;
    logic [SLOT_W-1:0] free_at, complete_at;
    logic [COUNT_W-1:0] count_busy;

    wire route_valid, route_ready, route_input_ready;
    wire [1:0] route_status;
    wire [SLOT_W-1:0] route_slot;
    wire [EPOCH_W-1:0] response_epoch;
    wire [ROW_W-1:0] response_row;
    wire route_tagged;
    wire [11:0] route_vid;
    wire [31:0] route_ip;
    wire group_address = s_destination[31:28] == 4'he;
    wire direct_bad_vid = s_tagged && s_vid == 12'hfff;
    wire valid_group = group_address && s_allow_multicast && !direct_bad_vid;
    wire [47:0] group_mac = {24'h01005e,1'b0,s_destination[22:0]};

    assign s_ready = !rst && free_found && (group_address || route_input_ready);
    assign m_valid = !rst && (held || complete_found);
    wire accepted = s_valid && s_ready;
    wire retired = m_valid && m_ready;
    wire [SLOT_W-1:0] output_index = held ? held_at : complete_at;
    assign m_tag = m_valid ? tag[output_index] : '0;
    assign {m_tagged,m_vid} = m_valid ? vlan_scope[output_index] : '0;
    assign m_destination = m_valid ? destination[output_index] : '0;
    assign m_next_hop = m_valid ? next_hop[output_index] : '0;
    assign m_mac = m_valid ? link_address[output_index] : '0;
    assign m_status = m_valid ? status[output_index] : '0;
    assign m_row = m_valid ? route_row[output_index] : '0;
    assign m_epoch = m_valid ? route_epoch[output_index] : '0;
    assign m_start_tick = m_valid ? start_tick[output_index] : '0;
    assign m_finish_tick = m_valid ? finish_tick[output_index] : '0;
    assign occupied = count_busy;

    assign arp_request_valid = !rst && route_valid && route_status == 0;
    assign {arp_request_tagged,arp_request_vid} = {route_tagged,route_vid};
    assign arp_request_ip = route_ip;
    assign arp_request_slot = route_slot;
    assign route_ready = !rst && (route_status != 0 || arp_request_ready);
    assign arp_result_ready = !rst;
    wire route_arrived = route_valid && route_ready;
    wire arp_arrived = arp_result_valid && arp_result_ready;
    wire arp_expected = int'(arp_result_slot) < SLOTS && state[arp_result_slot] == WAIT_NEIGHBOR;

    eth_ipv4_route #(.ROWS(ROWS), .COOKIE_W(SLOT_W), .EPOCH_W(EPOCH_W)) routes (
        .clk, .rst, .s_valid(accepted && !group_address), .s_ready(route_input_ready),
        .s_tagged, .s_vid, .s_destination, .s_cookie(free_at),
        .m_valid(route_valid), .m_ready(route_ready), .m_status(route_status), .m_cookie(route_slot),
        .m_epoch(response_epoch), .m_tagged(route_tagged), .m_vid(route_vid),
        .m_next_hop(route_ip), .m_row(response_row),
        .cfg_valid, .cfg_ready, .cfg_row, .cfg_enabled, .cfg_tagged,
        .cfg_vid, .cfg_network, .cfg_prefix, .cfg_reject, .cfg_gateway,
        .cfg_error, .commit_valid, .commit_ready, .active_epoch
    );

    function automatic logic [SLOT_W-1:0] step(input logic [SLOT_W-1:0] current);
        step = current == SLOT_W'(SLOTS-1) ? '0 : current + 1'b1;
    endfunction

    function automatic logic [31:0] increment(input logic [31:0] count);
        increment = (&count) ? count : count + 1'b1;
    endfunction

    function automatic logic [3:0] map_neighbor(input logic [2:0] code);
        case (code)
            0: map_neighbor = OK;
            1: map_neighbor = ARP_TIMEOUT;
            2: map_neighbor = ARP_BUSY;
            default: map_neighbor = ARP_BAD_REPLY;
        endcase
    endfunction

    always @* begin : find_slots
        integer allocate_at, retire_at;
        free_found = 1'b0;
        complete_found = 1'b0;
        free_at = '0;
        complete_at = '0;
        count_busy = '0;
        for (integer offset = 0; offset < SLOTS; offset++) begin
            allocate_at = int'(allocate_cursor) + offset;
            retire_at = int'(retire_cursor) + offset;
            if (allocate_at >= SLOTS)
                allocate_at = allocate_at - SLOTS;
            if (retire_at >= SLOTS)
                retire_at = retire_at - SLOTS;
            if (!free_found && state[allocate_at] == FREE) begin
                free_found = 1'b1;
                free_at = SLOT_W'(allocate_at);
            end
            if (!complete_found && state[retire_at] == COMPLETE) begin
                complete_found = 1'b1;
                complete_at = SLOT_W'(retire_at);
            end
            if (state[offset] != FREE)
                count_busy = count_busy + 1'b1;
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            for (integer slot = 0; slot < SLOTS; slot++)
                state[slot] <= FREE;
            allocate_cursor <= '0;
            retire_cursor <= '0;
            held_at <= '0;
            held <= 1'b0;
            unexpected_neighbor_replies <= '0;
            successful_lookups <= '0;
            failed_lookups <= '0;
        end else begin
            if (accepted) begin
                allocate_cursor <= step(free_at);
                tag[free_at] <= s_tag;
                vlan_scope[free_at] <= {s_tagged,s_tagged ? s_vid : 12'd0};
                destination[free_at] <= s_destination;
                start_tick[free_at] <= clock_tick;
                if (group_address) begin
                    state[free_at] <= COMPLETE;
                    route_row[free_at] <= '1;
                    route_epoch[free_at] <= active_epoch;
                    status[free_at] <= direct_bad_vid ? BAD_DESTINATION :
                                       valid_group ? OK : MULTICAST_DENIED;
                    next_hop[free_at] <= valid_group ? s_destination : '0;
                    link_address[free_at] <= valid_group ? group_mac : '0;
                    finish_tick[free_at] <= clock_tick;
                end else begin
                    state[free_at] <= ROUTE;
                end
            end
            if (route_arrived) begin
                route_row[route_slot] <= response_row;
                route_epoch[route_slot] <= response_epoch;
                state[route_slot] <= route_status == 0 ? WAIT_NEIGHBOR : COMPLETE;
                next_hop[route_slot] <= route_ip;
                if (route_status != 0) begin
                    status[route_slot] <= {2'd0,route_status};
                    link_address[route_slot] <= '0;
                    finish_tick[route_slot] <= clock_tick;
                end
            end
            if (arp_arrived) begin
                if (arp_expected) begin
                    state[arp_result_slot] <= COMPLETE;
                    status[arp_result_slot] <= map_neighbor(arp_result_status);
                    link_address[arp_result_slot] <= arp_result_status == 0 ? arp_result_mac : '0;
                    finish_tick[arp_result_slot] <= clock_tick;
                end else begin
                    unexpected_neighbor_replies <= increment(unexpected_neighbor_replies);
                end
            end
            if (m_valid && !m_ready) begin
                held <= 1'b1;
                held_at <= output_index;
            end
            if (retired) begin
                held <= 1'b0;
                state[output_index] <= FREE;
                retire_cursor <= step(output_index);
                if (m_status == OK)
                    successful_lookups <= increment(successful_lookups);
                else
                    failed_lookups <= increment(failed_lookups);
            end
        end
    end

`ifdef FORMAL_ROUTE_PIPELINE
    logic started = 1'b0;
    always_ff @(posedge clk) begin
        started <= 1'b1;
        if (!started)
            assume (rst);
        if (started && !rst && !$past(rst)) begin
            if (route_arrived)
                assert (state[route_slot] == ROUTE);
            if (m_valid)
                assert (state[output_index] == COMPLETE);
            if ($past(m_valid && !m_ready))
                assert (m_valid && $stable({m_tag,m_tagged,m_vid,m_destination,m_next_hop,m_mac,
                                            m_status,m_row,m_epoch,m_start_tick,m_finish_tick}));
            if (accepted && retired)
                assert (free_at != output_index);
            if (arp_arrived && arp_expected && route_arrived)
                assert (arp_result_slot != route_slot);
            cover (accepted && retired);
            cover (arp_arrived && arp_expected);
            cover (arp_arrived && !arp_expected);
        end
    end
`endif
endmodule

`default_nettype wire
