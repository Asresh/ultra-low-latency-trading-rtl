`timescale 1ns/1ps

// The Python driver supplies every edge and checks every sampled output.
module tb_best_quote_selector #(
    parameter integer INSTRUMENTS = 4,
    parameter integer VENUES = 4,
    parameter integer PRICE_W = 20,
    parameter integer QTY_W = 14,
    parameter integer TIME_W = 16,
    parameter integer SEQ_W = 12,
    parameter integer TAG_W = 16,
    localparam integer INST_W = INSTRUMENTS > 1 ? $clog2(INSTRUMENTS+1) : 1,
    localparam integer VENUE_W = VENUES > 1 ? $clog2(VENUES+1) : 1
);
    reg clk = 0;
    reg rst;
    reg [TIME_W-1:0] now_tick;
    reg cmd_valid, ack_ready, query_valid, result_ready;
    reg [2:0] cmd_op;
    reg [INST_W-1:0] cmd_instrument, query_instrument;
    reg [VENUE_W-1:0] cmd_venue;
    reg [SEQ_W-1:0] cmd_sequence;
    reg [PRICE_W-1:0] cmd_bid_price, cmd_ask_price;
    reg [QTY_W-1:0] cmd_bid_qty, cmd_ask_qty;
    reg [TAG_W-1:0] cmd_tag, query_tag;
    reg [TIME_W-1:0] query_max_age;

    wire cmd_ready, ack_valid, query_ready, result_valid;
    wire [3:0] ack_code, result_code;
    wire [TAG_W-1:0] ack_tag, result_tag;
    wire [2:0] result_state;
    wire [TIME_W-1:0] result_tick, result_bid_age, result_ask_age;
    wire [VENUES-1:0] result_active, result_stale;
    wire result_bid_valid, result_ask_valid;
    wire [VENUE_W-1:0] result_bid_venue, result_ask_venue;
    wire [PRICE_W-1:0] result_bid_price, result_ask_price;
    wire [QTY_W-1:0] result_bid_qty, result_ask_qty;
    wire [SEQ_W-1:0] result_bid_sequence, result_ask_sequence;
    wire signed [PRICE_W:0] result_spread;

    best_quote_selector #(.INSTRUMENTS(INSTRUMENTS), .VENUES(VENUES),
        .PRICE_W(PRICE_W), .QTY_W(QTY_W), .TIME_W(TIME_W), .SEQ_W(SEQ_W),
        .TAG_W(TAG_W)) dut (.*);

    // Emit canonical zeros for payloads outside valid cycles. The cycle number
    // gives the independent checker a precise diagnostic if an edge goes missing.
    task automatic print_sample(input integer trace, input integer cycle_number);
        logic show_ack, show_result;
        begin
            show_ack = !rst && ack_valid;
            show_result = !rst && result_valid;
            $fwrite(trace, "%0d %0d %0d %0d ", cycle_number, cmd_ready, query_ready, show_ack);
            if (show_ack)
                $fwrite(trace, "%0d %0d ", ack_code, ack_tag);
            else
                $fwrite(trace, "0 0 ");
            $fwrite(trace, "%0d ", show_result);
            if (show_result)
                $fwrite(trace, "%0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d\n",
                    result_tag, result_code, result_state, result_tick,
                    result_active, result_stale,
                    result_bid_valid, result_bid_venue, result_bid_price,
                    result_bid_qty, result_bid_age, result_bid_sequence,
                    result_ask_valid, result_ask_venue, result_ask_price,
                    result_ask_qty, result_ask_age, result_ask_sequence,
                    result_spread);
            else
                $fwrite(trace, "0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n");
        end
    endtask

    // Drive one whole row before the rising edge, then sample the settled bus.
    task automatic drive_edge(input integer trace, input integer cycle_number);
        begin
            #2;
            print_sample(trace, cycle_number);
            clk = 1;
            #2;
            clk = 0;
        end
    endtask

    string input_path;
    string output_path;
    integer source, trace, cycle_number, count;
    initial begin
        if (!$value$plusargs("input=%s", input_path) ||
            !$value$plusargs("output=%s", output_path))
            $fatal(1, "pass +input= and +output= paths");
        source = $fopen(input_path, "r");
        trace = $fopen(output_path, "w");
        if (source == 0 || trace == 0)
            $fatal(1, "could not open driver or trace");
        cycle_number = 0;
        while (!$feof(source)) begin
            count = $fscanf(source,
                "%d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d\n",
                now_tick, rst, ack_ready, result_ready,
                cmd_valid, cmd_op, cmd_instrument, cmd_venue, cmd_sequence,
                cmd_bid_price, cmd_ask_price, cmd_bid_qty, cmd_ask_qty, cmd_tag,
                query_valid, query_instrument, query_max_age, query_tag);
            if (count == 18) begin
                if (cycle_number == 0 && !rst)
                    $fatal(1, "stimulus must begin with reset");
                drive_edge(trace, cycle_number);
                cycle_number = cycle_number + 1;
            end else if (!$feof(source) || count >= 0)
                $fatal(1, "malformed vector at cycle %0d (%0d columns)", cycle_number, count);
        end
        if (cycle_number == 0)
            $fatal(1, "empty driver");
        $fclose(source);
        $fclose(trace);
        $display("Simulated %0d cycles with %0d instruments and %0d venues",
                 cycle_number, INSTRUMENTS, VENUES);
        $finish;
    end
endmodule
