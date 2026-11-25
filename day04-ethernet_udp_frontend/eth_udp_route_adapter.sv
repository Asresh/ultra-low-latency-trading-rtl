`timescale 1ns/1ps
`default_nettype none
`include "frontend_codes.svh"
`include "routed_codes.svh"

// Resolve/address each native TX descriptor. Both lanes can be outstanding
// independently. On a pre-transmit rejection, drain that descriptor's body.
module eth_udp_route_adapter #(
    parameter logic [47:0] LOCAL_MAC = 48'h0200_0000_000a,
    parameter logic [31:0] LOCAL_IP = 32'hc000_020a,
    parameter integer ROW_W = 4,
    parameter integer EPOCH_W = 16,
    parameter integer MAX_PAYLOAD = 1472
) (
    input  wire clk,
    input  wire rst,
    input  wire [63:0] tick,
    input  wire [EPOCH_W-1:0] active_epoch,
    input  wire route_commit,
    input  wire [1:0] tx_desc_valid,
    output wire [1:0] tx_desc_ready,
    input  wire [639:0] tx_desc_data,
    input  wire [1:0] tx_allow_multicast,
    input  wire [1:0] tx_require_current_epoch,
    input  wire [1:0] tx_payload_valid,
    output wire [1:0] tx_payload_ready,
    input  wire [1:0] tx_payload_last,

    output wire route_valid,
    input  wire route_ready,
    output wire route_lane,
    output wire route_tagged,
    output wire [11:0] route_vid,
    output wire [31:0] route_destination,
    output wire route_allow_multicast,
    input  wire resolved_valid,
    output wire resolved_ready,
    input  wire resolved_lane,
    input  wire [3:0] resolved_status,
    input  wire [47:0] resolved_mac,
    input  wire [31:0] resolved_next_hop,
    input  wire [ROW_W-1:0] resolved_row,
    input  wire [EPOCH_W-1:0] resolved_epoch,

    output wire [1:0] native_desc_valid,
    input  wire [1:0] native_desc_ready,
    output wire [639:0] native_desc_data,
    output wire [1:0] native_payload_valid,
    input  wire [1:0] native_payload_ready,
    input  wire native_done_valid,
    output wire native_done_ready,
    input  wire [127:0] native_done,

    output wire tx_status_valid,
    input  wire tx_status_ready,
    output wire [127:0] tx_status_data,
    output wire [31:0] tx_status_next_hop,
    output wire [ROW_W-1:0] tx_status_route_row,
    output wire [EPOCH_W-1:0] tx_status_epoch
);
    localparam integer QUERY_W = 46;
    localparam integer REPORT_W = 160+ROW_W+EPOCH_W;
    localparam logic [2:0] IDLE = 0, REQUEST = 1, WAIT_ROUTE = 2, NATIVE_DESC = 3,
                           NATIVE_BODY = 4, NATIVE_DONE = 5, DRAIN = 6, REPORT = 7;

    wire [1:0] request_valid, request_ready, report_valid, report_ready, native_wait;
    wire [2*QUERY_W-1:0] request_words;
    wire [2*REPORT_W-1:0] report_words;
    wire unused_status_lane;
    assign resolved_ready = !rst;
    assign native_done_ready = !rst && native_wait[native_done[112]];

    eth_packet_arbiter #(.HIGH_BURST(1), .WIDTH(QUERY_W)) choose_request (
        .clk, .rst, .in_valid(request_valid), .in_ready(request_ready), .in_last(2'b11),
        .in_data(request_words), .out_valid(route_valid), .out_ready(route_ready), .out_lane(route_lane),
        .out_data({route_allow_multicast,route_tagged,route_vid,route_destination})
    );
    eth_packet_arbiter #(.HIGH_BURST(1), .WIDTH(REPORT_W)) choose_report (
        .clk, .rst, .in_valid(report_valid), .in_ready(report_ready), .in_last(2'b11),
        .in_data(report_words), .out_valid(tx_status_valid), .out_ready(tx_status_ready),
        .out_lane(unused_status_lane),
        .out_data({tx_status_data,tx_status_next_hop,tx_status_route_row,tx_status_epoch})
    );

    for (genvar lane = 0; lane < 2; lane++) begin : g_lane
        localparam logic LANE = 1'(lane);
        logic [2:0] stage;
        logic [159:0] descriptor;
        logic [31:0] cookie;
        logic [47:0] destination_mac;
        logic [31:0] next_hop;
        logic [ROW_W-1:0] row;
        logic [EPOCH_W-1:0] epoch;
        logic allow_group, require_current, invalidated;
        logic [127:0] report;
        wire [159:0] offered_descriptor = tx_desc_data[320*lane+:160];
        wire [31:0] offered_cookie = tx_desc_data[320*lane+288+:32];
        wire [127:0] unused_address_fields = tx_desc_data[320*lane+160+:128];
        wire descriptor_invalid = offered_descriptor[7] ||
            offered_descriptor[31:24] == 0 || offered_descriptor[15:8] != 0 ||
            offered_descriptor[3:0] != 0 || offered_descriptor[95:80] > 16'(MAX_PAYLOAD);
        wire has_body = descriptor[95:80] != 0;
        wire stale = require_current && (invalidated || route_commit || epoch != active_epoch);
        wire resolved = resolved_valid && resolved_ready && resolved_lane == LANE;
        wire descriptor_transfer = native_desc_valid[lane] && native_desc_ready[lane];
        wire final_body = tx_payload_valid[lane] && tx_payload_ready[lane] && tx_payload_last[lane];
        wire phase_routes = stage == WAIT_ROUTE && resolved;
        wire completion = native_done_valid && native_done_ready && native_done[112] == LANE;
        wire [7:0] failure = 8'd32 + 8'(resolved_status);

        assign tx_desc_ready[lane] = !rst && stage == IDLE;
        assign request_valid[lane] = !rst && stage == REQUEST;
        assign request_words[QUERY_W*lane+:QUERY_W] = {
            allow_group,descriptor[6],descriptor[75:64],descriptor[159:128]
        };
        // Both sides are private wires. A descriptor pulse is generated only
        // on a current-epoch native acceptance edge, so a later commit never
        // changes a held public output or revokes a native payload already sent.
        assign native_desc_valid[lane] = !rst && stage == NATIVE_DESC &&
                                        !stale && native_desc_ready[lane];
        assign native_desc_data[320*lane+:320] = {
            cookie,destination_mac,LOCAL_MAC,LOCAL_IP,descriptor[159:0]
        };
        assign native_payload_valid[lane] = !rst && stage == NATIVE_BODY && tx_payload_valid[lane];
        assign tx_payload_ready[lane] = !rst &&
            (stage == DRAIN || (stage == NATIVE_BODY && native_payload_ready[lane]));
        assign native_wait[lane] = stage == NATIVE_DONE;
        assign report_valid[lane] = !rst && stage == REPORT;
        assign report_words[REPORT_W*lane+:REPORT_W] = {report,next_hop,row,epoch};

        function automatic logic [127:0] failure_report(
            input logic [7:0] code,
            input logic [31:0] report_cookie
        );
            failure_report = {code,7'd0,LANE,report_cookie,16'd0,tick};
        endfunction

        always_ff @(posedge clk) begin
            if (rst) begin
                stage <= IDLE;
                descriptor <= '0;
                cookie <= '0;
                destination_mac <= '0;
                next_hop <= '0;
                row <= '1;
                epoch <= '0;
                allow_group <= 0;
                require_current <= 0;
                invalidated <= 0;
                report <= '0;
            end else begin
                // Sticky invalidation covers an epoch counter that wraps
                // before an old neighbor resolution becomes available.
                if (route_commit && (stage == WAIT_ROUTE ||
                                    (stage == REQUEST && request_ready[lane])))
                    invalidated <= 1'b1;
                case (stage)
                    IDLE: if (tx_desc_valid[lane] && tx_desc_ready[lane]) begin
                        descriptor <= offered_descriptor;
                        cookie <= offered_cookie;
                        allow_group <= tx_allow_multicast[lane];
                        require_current <= tx_require_current_epoch[lane];
                        invalidated <= 0;
                        row <= '1;
                        next_hop <= '0;
                        epoch <= active_epoch;
                        if (descriptor_invalid) begin
                            report <= failure_report(`FE_TX_DESCRIPTOR, offered_cookie);
                            stage <= offered_descriptor[95:80] == 0 ? REPORT : DRAIN;
                        end else begin
                            stage <= REQUEST;
                        end
                    end
                    REQUEST: if (request_ready[lane])
                        stage <= WAIT_ROUTE;
                    WAIT_ROUTE: if (phase_routes) begin
                        row <= resolved_row;
                        next_hop <= resolved_next_hop;
                        epoch <= resolved_epoch;
                        if (resolved_status == 0) begin
                            destination_mac <= resolved_mac;
                            stage <= NATIVE_DESC;
                        end else begin
                            report <= failure_report(failure, cookie);
                            stage <= has_body ? DRAIN : REPORT;
                        end
                    end
                    NATIVE_DESC: if (stale) begin
                        report <= failure_report(`ROUTED_TX_STALE_EPOCH, cookie);
                        stage <= has_body ? DRAIN : REPORT;
                    end else if (descriptor_transfer) begin
                        stage <= has_body ? NATIVE_BODY : NATIVE_DONE;
                    end
                    NATIVE_BODY: if (final_body)
                        stage <= NATIVE_DONE;
                    NATIVE_DONE: if (completion) begin
                        report <= native_done;
                        stage <= REPORT;
                    end
                    DRAIN: if (final_body)
                        stage <= REPORT;
                    REPORT: if (report_ready[lane])
                        stage <= IDLE;
                    default: stage <= IDLE;
                endcase
            end
        end
    end
endmodule

`default_nettype wire
