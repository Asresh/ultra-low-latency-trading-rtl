`timescale 1ns/1ps
`default_nettype none

module tb_quote_sweep_selector #(
    parameter integer INSTRUMENTS = 4,
    parameter integer VENUES = 4,
    parameter integer PRICE_W = 20,
    parameter integer QTY_W = 14,
    parameter integer TIME_W = 16,
    parameter integer SEQ_W = 12,
    parameter integer TAG_W = 16,
    parameter integer FEE_W = 12,
    parameter integer EPOCH_W = 8,
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

    quote_sweep_selector #(
        .INSTRUMENTS(INSTRUMENTS), .VENUES(VENUES), .PRICE_W(PRICE_W),
        .QTY_W(QTY_W), .TIME_W(TIME_W), .SEQ_W(SEQ_W), .TAG_W(TAG_W),
        .FEE_W(FEE_W), .EPOCH_W(EPOCH_W)
    ) dut (.*);

    task automatic print_sample(input integer output_file, input integer cycle);
        logic show_ack, show_result;
        begin
            show_ack = !rst && ack_valid;
            show_result = !rst && result_valid;
            $fwrite(output_file, "%0d %0d %0d %0d %0d %0d %0d %0d ", cycle,
                cmd_ready, cfg_ready, commit_ready, !rst && cfg_error,
                rst ? EPOCH_W'(0) : policy_epoch, query_ready, show_ack);
            if (show_ack)
                $fwrite(output_file, "%0d %0d ", ack_code, ack_tag);
            else
                $fwrite(output_file, "0 0 ");
            $fwrite(output_file, "%0d ", show_result);
            if (show_result)
                $fwrite(output_file, "%0d %0d %0d %0d %0d %0d %0d %0d %0d %0h\n",
                    result_tag, result_tick, result_epoch, result_status,
                    result_eligible, result_stale, result_count,
                    result_quantity, result_value, result_children);
            else
                $fwrite(output_file, "0 0 0 0 0 0 0 0 0 0\n");
        end
    endtask

    string input_path, output_path;
    integer source, trace, cycle_number, fields;
    initial begin
        if (!$value$plusargs("input=%s", input_path) ||
            !$value$plusargs("output=%s", output_path))
            $fatal(1, "pass +input= and +output= paths");
        source = $fopen(input_path, "r");
        trace = $fopen(output_path, "w");
        if (!source || !trace)
            $fatal(1, "could not open driver or trace");

        cycle_number = 0;
        while (!$feof(source)) begin
            fields = $fscanf(source,
                "%d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d\n",
                now_tick, rst, ack_ready, result_ready,
                cmd_valid, cmd_op, cmd_instrument, cmd_venue, cmd_sequence,
                cmd_bid_price, cmd_ask_price, cmd_bid_qty, cmd_ask_qty, cmd_tag,
                cfg_valid, cfg_venue, cfg_enabled, cfg_cap, cfg_fee, commit_valid,
                query_valid, query_instrument, query_tag, query_buy, query_quantity,
                query_limit, query_max_age, query_venues, query_all_or_none);
            if (fields == 29) begin
                if (cycle_number == 0 && !rst)
                    $fatal(1, "stimulus must begin with reset");
                #2;
                print_sample(trace, cycle_number);
                clk = 1;
                #2;
                clk = 0;
                cycle_number++;
            end else if (!$feof(source) || fields >= 0)
                $fatal(1, "malformed vector at cycle %0d (%0d columns)", cycle_number, fields);
        end
        if (cycle_number == 0)
            $fatal(1, "empty driver");
        $fclose(source);
        $fclose(trace);
        $display("Compared sweep trace at %0d cycles, %0d instruments, %0d venues",
                 cycle_number, INSTRUMENTS, VENUES);
        $finish;
    end

    string waveform_path;
    initial begin
        if ($value$plusargs("waveform=%s", waveform_path)) begin
            $dumpfile(waveform_path);
            $dumpvars(1, tb_quote_sweep_selector);
        end
    end
endmodule

`default_nettype wire
