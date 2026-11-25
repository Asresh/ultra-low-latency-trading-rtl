`timescale 1ns/1ps
`default_nettype none

module tb_quote_reserved_sweep #(
    parameter integer INSTRUMENTS = 4,
    parameter integer VENUES = 4,
    parameter integer PRICE_W = 20,
    parameter integer QTY_W = 14,
    parameter integer TIME_W = 16,
    parameter integer SEQ_W = 12,
    parameter integer TAG_W = 16,
    parameter integer FEE_W = 12,
    parameter integer EPOCH_W = 8,
    parameter integer RES_SLOTS = 3,
    parameter integer REV_W = 4,
    parameter integer TOKEN_W = 3,
    localparam integer INST_W = $clog2(INSTRUMENTS+1),
    localparam integer VENUE_W = $clog2(VENUES+1),
    localparam integer REQUEST_W = QTY_W + $clog2(VENUES),
    localparam integer UNIT_W = (PRICE_W+1 > FEE_W ? PRICE_W+1 : FEE_W) + 1,
    localparam integer VALUE_W = UNIT_W + REQUEST_W,
    localparam integer CHILD_W = VENUE_W + PRICE_W + QTY_W + FEE_W + TIME_W + SEQ_W
);
    logic clk = 0;
    logic rst;
    logic [TIME_W-1:0] now_tick;
    logic cmd_valid, ack_ready;
    wire cmd_ready, ack_valid;
    logic [2:0] cmd_op;
    logic [INST_W-1:0] cmd_instrument, query_instrument;
    logic [VENUE_W-1:0] cmd_venue, cfg_venue;
    logic [SEQ_W-1:0] cmd_sequence;
    logic [PRICE_W-1:0] cmd_bid_price, cmd_ask_price, query_limit;
    logic [QTY_W-1:0] cmd_bid_qty, cmd_ask_qty, cfg_cap;
    logic [TAG_W-1:0] cmd_tag, query_tag;
    wire [3:0] ack_code;
    wire [TAG_W-1:0] ack_tag;

    logic cfg_valid, cfg_enabled, commit_valid;
    logic signed [FEE_W-1:0] cfg_fee;
    wire cfg_ready, cfg_error, commit_ready;
    wire [EPOCH_W-1:0] policy_epoch, result_epoch;

    logic query_valid, query_buy, query_all_or_none, result_ready;
    wire query_ready, result_valid;
    logic [REQUEST_W-1:0] query_quantity;
    logic [TIME_W-1:0] query_max_age;
    logic [VENUES-1:0] query_venues;

    wire [3:0] result_status;
    wire [TAG_W-1:0] result_tag;
    wire [TIME_W-1:0] result_tick;
    wire [VENUES-1:0] result_eligible, result_stale;
    wire [VENUE_W-1:0] result_count;
    wire [REQUEST_W-1:0] result_quantity;
    wire signed [VALUE_W-1:0] result_value;
    wire [VENUES*CHILD_W-1:0] result_children;
    wire [VENUE_W-1:0] first_venue = result_children[CHILD_W-1 -: VENUE_W];

    localparam integer SLOT_W = $clog2(RES_SLOTS+1);
    localparam integer HOLD_RESULT_W =
        4+TAG_W+TIME_W+EPOCH_W+2*VENUES+VENUE_W+REQUEST_W+VALUE_W+VENUES*CHILD_W+
        3+SLOT_W+TOKEN_W+TIME_W;
    localparam integer HOLD_GRANT_W =
        3+1+TAG_W+SLOT_W+TOKEN_W+INST_W+1+VENUES*CHILD_W;
    logic [TIME_W-1:0] query_ttl;
    wire [2:0] result_reservation;
    wire [SLOT_W-1:0] result_slot;
    wire [TOKEN_W-1:0] result_generation;
    wire [TIME_W-1:0] result_expires;
    logic action_valid, action_claim, grant_ready;
    logic [TAG_W-1:0] action_tag;
    logic [SLOT_W-1:0] action_slot;
    logic [TOKEN_W-1:0] action_generation;
    wire action_ready, grant_valid, grant_applicable, grant_claim, grant_buy;
    wire [2:0] grant_status;
    wire [TAG_W-1:0] grant_tag;
    wire [SLOT_W-1:0] grant_slot, reservation_count;
    wire [TOKEN_W-1:0] grant_generation;
    wire [INST_W-1:0] grant_instrument;
    wire [VENUES*CHILD_W-1:0] grant_children;
    wire [31:0] reserved_total, dispatched_total, released_total, invalidated_total, revoked_grants;

    quote_reserved_sweep #(
        .INSTRUMENTS(INSTRUMENTS), .VENUES(VENUES), .PRICE_W(PRICE_W), .QTY_W(QTY_W),
        .TIME_W(TIME_W), .SEQ_W(SEQ_W), .TAG_W(TAG_W), .FEE_W(FEE_W), .EPOCH_W(EPOCH_W),
        .RES_SLOTS(RES_SLOTS), .REV_W(REV_W), .TOKEN_W(TOKEN_W)
    ) dut (.*);

    wire [HOLD_RESULT_W-1:0] result_payload = {
        result_status,result_tag,result_tick,result_epoch,result_eligible,result_stale,
        result_count,result_quantity,result_value,result_children,result_reservation,
        result_slot,result_generation,result_expires
    };
    wire [HOLD_GRANT_W-1:0] grant_payload = {
        grant_status,grant_claim,grant_tag,grant_slot,grant_generation,
        grant_instrument,grant_buy,grant_children
    };
    logic [HOLD_RESULT_W-1:0] prior_result;
    logic [HOLD_GRANT_W-1:0] prior_grant;
    bit stalled_result = 0, stalled_grant = 0, prior_applicable;
    integer output_file;

    task automatic sample(input integer cycle);
        bit show_ack, show_result, show_grant;
        begin
            if (!rst) begin
                if (stalled_result && (!result_valid || prior_result !== result_payload))
                    $fatal(1,"reserved result changed while stalled at cycle %0d",cycle);
                if (stalled_grant && (!grant_valid || prior_grant !== grant_payload))
                    $fatal(1,"grant payload changed while stalled at cycle %0d",cycle);
                if (stalled_grant && !prior_applicable && grant_applicable)
                    $fatal(1,"revoked grant revived at cycle %0d",cycle);
            end
            stalled_result = !rst && result_valid && !result_ready;
            stalled_grant = !rst && grant_valid && !grant_ready;
            prior_result = result_payload;
            prior_grant = grant_payload;
            prior_applicable = grant_applicable;

            show_ack = !rst && ack_valid;
            show_result = !rst && result_valid;
            show_grant = !rst && grant_valid;
            $fwrite(output_file,"%0d %0d %0d %0d %0d %0d %0d %0d ",cycle,
                cmd_ready,cfg_ready,commit_ready,!rst && cfg_error,rst ? EPOCH_W'(0) : policy_epoch,
                query_ready, action_ready);
            if (show_ack)
                $fwrite(output_file,"1 %0d %0d ",ack_code,ack_tag);
            else
                $fwrite(output_file,"0 0 0 ");
            if (show_result)
                $fwrite(output_file,"1 %0d %0d %0d %0d %0d %0d %0d %0d %0d %0h %0d %0d %0d %0d ",
                    result_tag,result_tick,result_epoch,result_status,result_eligible,result_stale,
                    result_count,result_quantity,result_value,result_children,result_reservation,
                    result_slot,result_generation,result_expires);
            else
                $fwrite(output_file,"0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 ");
            if (show_grant)
                $fwrite(output_file,"1 %0d %0d %0d %0d %0d %0d %0d %0d %0h ",
                    grant_applicable,grant_status,grant_claim,grant_tag,grant_slot,grant_generation,
                    grant_instrument,grant_buy,grant_children);
            else
                $fwrite(output_file,"0 0 0 0 0 0 0 0 0 0 ");
            if (rst)
                $fwrite(output_file,"0 0 0 0 0 0\n");
            else
                $fwrite(output_file,"%0d %0d %0d %0d %0d %0d\n",reservation_count,reserved_total,
                    dispatched_total,released_total,invalidated_total,revoked_grants);
        end
    endtask

    string input_path, output_path;
    integer source, cycle_number, fields;
    initial begin
        if (!$value$plusargs("input=%s", input_path) ||
            !$value$plusargs("output=%s", output_path))
            $fatal(1,"pass +input= and +output= paths");
        source = $fopen(input_path, "r");
        output_file = $fopen(output_path, "w");
        if (!source || !output_file)
            $fatal(1,"could not open driver or trace");

        cycle_number = 0;
        while (!$feof(source)) begin
            fields = $fscanf(source,
                "%d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d\n",
                now_tick,rst,ack_ready,result_ready,
                cmd_valid,cmd_op,cmd_instrument,cmd_venue,cmd_sequence,
                cmd_bid_price,cmd_ask_price,cmd_bid_qty,cmd_ask_qty,cmd_tag,
                cfg_valid,cfg_venue,cfg_enabled,cfg_cap,cfg_fee,commit_valid,
                query_valid,query_instrument,query_tag,query_buy,query_quantity,
                query_limit,query_max_age,query_venues,query_all_or_none,
                query_ttl,grant_ready,action_valid,action_claim,action_tag,action_slot,action_generation);
            if (fields == 36) begin
                if (cycle_number == 0 && !rst)
                    $fatal(1,"stimulus must start with reset");
                #2;
                sample(cycle_number);
                clk = 1;
                #2;
                clk = 0;
                cycle_number++;
            end else if (!$feof(source) || fields >= 0)
                $fatal(1,"malformed reservation row at cycle %0d (%0d fields)",cycle_number,fields);
        end
        if (cycle_number == 0)
            $fatal(1,"empty driver");
        $fclose(source);
        $fclose(output_file);
        $display("Compared reservation trace at %0d cycles, %0d instruments, %0d venues, %0d tokens",
                 cycle_number,INSTRUMENTS,VENUES,RES_SLOTS);
        $finish;
    end

    string waveform_path;
    initial begin
        if ($value$plusargs("waveform=%s",waveform_path)) begin
            $dumpfile(waveform_path);
            $dumpvars(1,tb_quote_reserved_sweep);
        end
    end
endmodule

`default_nettype wire
