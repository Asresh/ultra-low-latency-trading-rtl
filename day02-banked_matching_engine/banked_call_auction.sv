`timescale 1ns/1ps
`default_nettype none
`include "auction_codes.svh"
`include "auction_bank_codes.svh"

// Each book keeps its own input/output credits. A scheduled closing marker
// goes behind that book's accepted prefix and ahead of all cutoff rejections.
module banked_call_auction #(
    parameter integer BANKS = 3,
    parameter integer PRICES = 16,
    parameter integer SLOTS = 16,
    parameter integer QTY_W = 16,
    parameter integer OWNER_W = 8,
    parameter integer EPOCH_W = 16,
    parameter integer TAG_W = 16,
    parameter integer SEQ_W = 16,
    parameter integer CLOCK_W = 48,
    parameter integer CMD_DEPTH = 4,
    parameter integer EVENT_DEPTH = 4,
    localparam integer BANK_W = $clog2(BANKS+1),
    localparam integer PRICE_W = $clog2(PRICES+1),
    localparam integer ID_W = $clog2(SLOTS+1),
    localparam integer SUM_W = QTY_W+ID_W,
    localparam integer COMMAND_W = 7+EPOCH_W+TAG_W+ID_W+OWNER_W+3*PRICE_W+QTY_W,
    localparam integer RESULT_W = 13+EPOCH_W+TAG_W+5*ID_W+2*OWNER_W+PRICE_W+3*SUM_W
) (
    input  wire clk,
    input  wire rst,
    input  wire s_valid,
    output wire s_ready,
    input  wire [BANK_W-1:0] s_bank,
    input  wire [COMMAND_W-1:0] s_command,
    output wire m_valid,
    input  wire m_ready,
    output wire [BANK_W-1:0] m_bank,
    output wire [SEQ_W-1:0] m_command_sequence,
    output wire [SEQ_W-1:0] m_event_sequence,
    output wire m_scheduled,
    output wire [1:0] m_wrapper_status,
    output wire [RESULT_W-1:0] m_result,
    input  wire ctl_valid,
    output wire ctl_ready,
    input  wire [1:0] ctl_op,
    input  wire [BANKS-1:0] ctl_mask,
    input  wire [BANKS*EPOCH_W-1:0] ctl_epochs,
    input  wire [CLOCK_W-1:0] ctl_cutoff,
    input  wire [TAG_W-1:0] ctl_tag,
    output wire ctl_report_valid,
    input  wire ctl_report_ready,
    output logic [1:0] ctl_report_op,
    output logic [1:0] ctl_report_status,
    output logic [BANKS-1:0] ctl_report_mask,
    output logic [TAG_W-1:0] ctl_report_tag,
    output logic [BANKS*3-1:0] ctl_report_detail,
    output logic [BANKS-1:0] ctl_report_armed,
    output logic [BANKS*CLOCK_W-1:0] ctl_report_cutoffs,
    output logic [CLOCK_W-1:0] clock_tick,
    output wire [BANKS-1:0] schedule_armed,
    output wire [BANKS-1:0] schedule_due,
    output wire [BANKS-1:0] schedule_queued
);
    localparam integer BANK_ADDR_W = BANKS > 1 ? $clog2(BANKS) : 1;
    localparam integer LANES = BANKS+1;
    localparam integer OUT_W = BANK_W+2*SEQ_W+3+RESULT_W;
    localparam integer IN_W = SEQ_W+3+COMMAND_W;
    localparam integer RESULT_SUFFIX_W = RESULT_W-9-EPOCH_W-TAG_W;

    wire [BANKS-1:0] ingress_ready;
    wire [LANES-1:0] lane_valid, lane_ready;
    wire [LANES*OUT_W-1:0] payload;
    wire [OUT_W-1:0] merged;
    wire [BANKS*CLOCK_W-1:0] deadlines;
    wire [BANKS*3-1:0] details;
    wire invalid_ready;
    wire bank_known = s_bank < BANK_W'(BANKS);
    wire [2:0] visible_op = s_command[COMMAND_W-1-:3];
    wire [EPOCH_W-1:0] visible_epoch = s_command[COMMAND_W-4-:EPOCH_W];
    wire [TAG_W-1:0] visible_tag = s_command[COMMAND_W-4-EPOCH_W-:TAG_W];
    wire [RESULT_W-1:0] bad_bank_result = {
        `AU_EV_REJECT, `AU_ERR_NONE, 1'b1, visible_epoch, visible_tag, RESULT_SUFFIX_W'(0)
    };
    logic control_held;
    wire fields_ok = (|ctl_mask) && ctl_op <= `AB_QUERY &&
        (ctl_op == `AB_ARM || ctl_cutoff == 0);
    wire [1:0] ctl_status = !fields_ok ? `AB_ADMIN_FIELDS :
        (|details) ? `AB_ADMIN_ROLLBACK : `AB_ADMIN_OK;
    wire manage = ctl_valid && ctl_ready && ctl_status == `AB_ADMIN_OK;

    assign s_ready = bank_known ? ingress_ready[BANK_ADDR_W'(s_bank)] : invalid_ready;
    assign ctl_ready = !rst && (!control_held || ctl_report_ready);
    assign ctl_report_valid = !rst && control_held;
    assign {m_bank, m_command_sequence, m_event_sequence, m_scheduled, m_wrapper_status, m_result} = merged;

    always_ff @(posedge clk) begin
        if (rst) begin
            clock_tick <= '0;
            control_held <= 0;
            {ctl_report_op,ctl_report_status,ctl_report_mask,ctl_report_tag,ctl_report_detail,
             ctl_report_armed,ctl_report_cutoffs} <= '0;
        end else begin
            if (!(&clock_tick))
                clock_tick <= clock_tick+1'b1;
            if (ctl_ready) begin
                control_held <= ctl_valid;
                if (ctl_valid) begin
                    {ctl_report_op,ctl_report_status,ctl_report_mask,ctl_report_tag,ctl_report_detail} <=
                        {ctl_op,ctl_status,ctl_mask,ctl_tag,details};
                    for (integer i = 0; i < BANKS; i++) begin
                        if (manage && ctl_mask[i] && ctl_op == `AB_ARM) begin
                            ctl_report_armed[i] <= 1;
                            ctl_report_cutoffs[i*CLOCK_W+:CLOCK_W] <= ctl_cutoff;
                        end else if (manage && ctl_mask[i] && ctl_op == `AB_CANCEL) begin
                            ctl_report_armed[i] <= 0;
                            ctl_report_cutoffs[i*CLOCK_W+:CLOCK_W] <= '0;
                        end else begin
                            ctl_report_armed[i] <= ctl_mask[i] && schedule_armed[i];
                            ctl_report_cutoffs[i*CLOCK_W+:CLOCK_W] <= ctl_mask[i] && schedule_armed[i] ?
                                deadlines[i*CLOCK_W+:CLOCK_W] : CLOCK_W'(0);
                        end
                    end
                end
            end
        end

    end

    for (genvar instrument = 0; instrument < BANKS; instrument++) begin : bank
        localparam logic [BANK_W-1:0] KEY = BANK_W'(instrument);
        logic collecting, armed, inserted;
        logic [EPOCH_W-1:0] open_epoch, arm_epoch;
        logic [CLOCK_W-1:0] deadline;
        logic [TAG_W-1:0] arm_tag;
        logic [SEQ_W-1:0] next_command, next_event, working_sequence;
        logic working_schedule;
        logic [2:0] detail;
        wire set_arm = manage && ctl_mask[instrument] && ctl_op == `AB_ARM;
        wire clear_arm = manage && ctl_mask[instrument] && ctl_op == `AB_CANCEL;
        wire due = armed && clock_tick >= deadline;
        wire auto_insert = due && !inserted;
        wire [EPOCH_W-1:0] expected_epoch = ctl_epochs[instrument*EPOCH_W+:EPOCH_W];

        wire request_valid, input_credit, worker_ready, worker_valid, event_credit;
        wire [IN_W-1:0] request;
        wire [COMMAND_W-1:0] command;
        wire is_auto;
        wire [1:0] reject_code;
        wire [SEQ_W-1:0] command_seq;
        wire [2:0] op;
        wire [EPOCH_W-1:0] epoch_arg;
        wire [TAG_W-1:0] tag_arg;
        wire [ID_W-1:0] order_id;
        wire [OWNER_W-1:0] account;
        wire side, market, carry, reference_half;
        wire [PRICE_W-1:0] price, low_tick, high_tick;
        wire [QTY_W-1:0] quantity;
        assign {command_seq,is_auto,reject_code,command} = request;
        assign {op,epoch_arg,tag_arg,order_id,account,side,market,carry,reference_half,
                price,low_tick,high_tick,quantity} = command;

        wire [1:0] input_reason = due ? `AB_CUTOFF :
            (armed && (visible_op == `AU_CLOSE || visible_op == `AU_ABORT)) ? `AB_LOCKED : `AB_ADMITTED;
        wire external_offer = s_valid && s_bank == KEY && !set_arm && !auto_insert;
        assign ingress_ready[instrument] = input_credit && !set_arm && !auto_insert;
        assign schedule_armed[instrument] = armed;
        assign schedule_due[instrument] = due;
        assign schedule_queued[instrument] = inserted;
        assign deadlines[instrument*CLOCK_W+:CLOCK_W] = deadline;
        assign details[instrument*3+:3] = detail;

        always @* begin
            detail = `AB_READY;
            if (fields_ok && ctl_mask[instrument]) begin
                if (ctl_op == `AB_ARM) begin
                    if (ctl_cutoff <= clock_tick)
                        detail = `AB_PAST;
                    else if (!collecting)
                        detail = `AB_INACTIVE;
                    else if (expected_epoch != open_epoch)
                        detail = `AB_EPOCH;
                    else if (armed)
                        detail = `AB_ARMED;
                    else if (request_valid || !worker_ready)
                        detail = `AB_BUSY;
                end else if (ctl_op == `AB_CANCEL) begin
                    if (!armed)
                        detail = `AB_UNARMED;
                    else if (expected_epoch != arm_epoch)
                        detail = `AB_EPOCH;
                    else if (due)
                        detail = `AB_PAST;
                end
            end
        end

        wire [COMMAND_W-1:0] automatic_command = {
            `AU_CLOSE, arm_epoch, arm_tag, (COMMAND_W-3-EPOCH_W-TAG_W)'(0)
        };
        wire [IN_W-1:0] input_payload = auto_insert ?
            {next_command,1'b1,`AB_ADMITTED,automatic_command} :
            {next_command,1'b0,input_reason,s_command};
        wire discard = request_valid && reject_code != `AB_ADMITTED && worker_ready;
        wire dispatch = request_valid && reject_code == `AB_ADMITTED && worker_ready;
        matching_fifo #(.WIDTH(IN_W),.DEPTH(CMD_DEPTH)) command_fifo (
            .clk,.rst,.s_valid(auto_insert || external_offer),.s_ready(input_credit),
            .s_payload(input_payload),.m_valid(request_valid),.m_ready(dispatch || (discard && event_credit)),
            .m_payload(request)
        );

        wire [3:0] kind, reason;
        wire last, out_side, out_market, out_carry, out_half;
        wire [EPOCH_W-1:0] epoch_out;
        wire [TAG_W-1:0] tag_out;
        wire [ID_W-1:0] first, second, count, carried, expired;
        wire [OWNER_W-1:0] first_owner, second_owner;
        wire [PRICE_W-1:0] clearing_tick;
        wire [SUM_W-1:0] volume, buy, sell;
        call_auction_engine #(
            .PRICES(PRICES),.SLOTS(SLOTS),.QTY_W(QTY_W),.OWNER_W(OWNER_W),.EPOCH_W(EPOCH_W),.TAG_W(TAG_W)
        ) auction (
            .clk,.rst,.s_valid(request_valid && reject_code == `AB_ADMITTED),.s_ready(worker_ready),
            .s_op(op),.s_epoch(epoch_arg),.s_tag(tag_arg),.s_id(order_id),.s_owner(account),
            .s_side(side),.s_market(market),.s_carry(carry),.s_reference_half(reference_half),
            .s_price(price),.s_low(low_tick),.s_high(high_tick),.s_quantity(quantity),
            .m_valid(worker_valid),.m_ready(event_credit),.m_kind(kind),.m_reason(reason),.m_last(last),
            .m_epoch(epoch_out),.m_tag(tag_out),.m_first(first),.m_second(second),
            .m_first_owner(first_owner),.m_second_owner(second_owner),
            .m_side(out_side),.m_market(out_market),.m_carry(out_carry),.m_reference_half(out_half),
            .m_price(clearing_tick),.m_quantity(volume),.m_buys(buy),.m_sells(sell),
            .m_count(count),.m_carried(carried),.m_expired(expired)
        );
        wire [RESULT_W-1:0] worker_result = {
            kind,reason,last,epoch_out,tag_out,first,second,first_owner,second_owner,
            out_side,out_market,out_carry,out_half,clearing_tick,volume,buy,sell,count,carried,expired
        };
        wire [RESULT_W-1:0] rejected = {
            `AU_EV_REJECT,`AU_ERR_NONE,1'b1,epoch_arg,tag_arg,RESULT_SUFFIX_W'(0)
        };
        wire [OUT_W-1:0] outgoing = discard ?
            {KEY,command_seq,next_event,1'b0,reject_code,rejected} :
            {KEY,working_sequence,next_event,working_schedule,`AB_ADMITTED,worker_result};
        matching_fifo #(.WIDTH(OUT_W),.DEPTH(EVENT_DEPTH)) events (
            .clk,.rst,.s_valid(worker_valid || discard),.s_ready(event_credit),.s_payload(outgoing),
            .m_valid(lane_valid[instrument]),.m_ready(lane_ready[instrument]),
            .m_payload(payload[instrument*OUT_W+:OUT_W])
        );

        always_ff @(posedge clk) begin
            if (rst) begin
                collecting <= 0;
                open_epoch <= '0;
                {armed,inserted,deadline,arm_epoch,arm_tag} <= '0;
                {next_command,next_event,working_sequence,working_schedule} <= '0;
            end else begin
                if ((auto_insert || external_offer) && input_credit)
                    next_command <= next_command+1'b1;
                if (dispatch)
                    {working_sequence,working_schedule} <= {command_seq,is_auto};
                if ((worker_valid || discard) && event_credit)
                    next_event <= next_event+1'b1;
                if (worker_valid && event_credit) begin
                    if (kind == `AU_EV_OPENED) begin
                        collecting <= 1;
                        open_epoch <= epoch_out;
                    end else if (kind == `AU_EV_CLEARING || kind == `AU_EV_ABORTED) begin
                        collecting <= 0;
                    end
                end
                if (set_arm) begin
                    armed <= 1;
                    inserted <= 0;
                    deadline <= ctl_cutoff;
                    arm_epoch <= open_epoch;
                    arm_tag <= ctl_tag;
                end else if (clear_arm || (worker_valid && event_credit && working_schedule && last)) begin
                    armed <= 0;
                    inserted <= 0;
                    deadline <= '0;
                end else if (auto_insert && input_credit) begin
                    inserted <= 1;
                end
            end
        end
`ifdef FORMAL_BANKED_AUCTION
`include "bank_schedule_properties.svh"
`endif
    end

    logic [SEQ_W-1:0] bad_sequence;
    matching_fifo #(.WIDTH(OUT_W),.DEPTH(EVENT_DEPTH)) invalid_bank_fifo (
        .clk,.rst,.s_valid(s_valid && !bank_known),.s_ready(invalid_ready),
        .s_payload({s_bank,bad_sequence,bad_sequence,1'b0,`AB_INVALID_KEY,bad_bank_result}),
        .m_valid(lane_valid[BANKS]),.m_ready(lane_ready[BANKS]),.m_payload(payload[BANKS*OUT_W+:OUT_W])
    );
    always_ff @(posedge clk) begin
        if (rst)
            bad_sequence <= '0;
        else if (s_valid && !bank_known && invalid_ready)
            bad_sequence <= bad_sequence+1'b1;
    end

    matching_arbiter #(.PORTS(LANES),.WIDTH(OUT_W)) egress (
        .clk,.rst,.s_valid(lane_valid),.s_ready(lane_ready),.s_payload(payload),
        .m_valid,.m_ready,.m_payload(merged)
    );

`ifdef FORMAL_BANKED_AUCTION
`include "cohort_properties.svh"
`endif
endmodule

`default_nettype wire
