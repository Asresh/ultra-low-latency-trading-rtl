`timescale 1ns/1ps
`default_nettype none

module tb_notional_pair;
    logic clk = 0;
    always #5 clk = ~clk;
    logic rst = 1, s_valid = 0, m_ready = 0;
    logic [163:0] s_request = '0;
    wire s_ready, m_valid;
    wire [212:0] m_reply;
    wire [1:0] outstanding;

    notional_pair dut (.*);

    logic [163:0] input_rows [0:1200];
    logic [212:0] expected_rows [0:1200];
    logic [15:0] tag, fee_bps, out_tag;
    logic [31:0] price, qty;
    logic [1:0] mode;
    logic sell, limit_enabled, out_side, inexact;
    logic [63:0] limit_value, gross, fee, cash;
    logic [2:0] status;
    logic [31:0] noise = 32'he6b00d8b;
    logic holding = 0;
    logic [212:0] held_reply;
    integer tape, fields, latency, rows = 0, cycles = 0;
    integer selected = 0, emitted = 0, blocked = 0, paired = 0;
    integer overlapped = 0, dispatching_both = 0;
    string source;

    // Capture one complete line of physical inputs and expected outputs.
    task automatic load_row;
        fields = $fscanf(tape,"%h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h",
                         tag,price,qty,fee_bps,mode,sell,limit_enabled,limit_value,
                         out_tag,status,out_side,gross,fee,cash,inexact,latency);
        if (fields != 16 && !$feof(tape)) $fatal(1,"Malformed numeric tape at %0d",rows);
        if (fields == 16) begin
            input_rows[rows] = {tag,price,qty,fee_bps,mode,sell,limit_enabled,limit_value};
            expected_rows[rows] = {out_tag,status,out_side,gross,fee,cash,inexact};
            rows++;
        end
    endtask

    // Observe both port transfers and compare the stable reply to the tape head.
    task automatic check_edge;
        logic take, retire;
        take = s_valid && s_ready;
        retire = m_valid && m_ready;
        if (outstanding !== 2'(selected-emitted))
            $fatal(1,"Outstanding count %0d does not match %0d",outstanding,selected-emitted);
        if (holding && (!m_valid || m_reply !== held_reply))
            $fatal(1,"Reply changed while stalled at cycle %0d",cycles);
        holding = m_valid && !m_ready;
        held_reply = m_reply;
        if (holding) blocked++;
        if (outstanding == 2) paired++;
        if (take && retire) overlapped++;
        if (take && outstanding == 1) dispatching_both++;
        if (take) selected++;
        if (retire) begin
            if (emitted >= selected || m_reply !== expected_rows[emitted])
                $fatal(1,"Parallel cores reordered or corrupted reply %0d",emitted);
            emitted++;
        end
        cycles++;
    endtask

    initial begin
        if (!$value$plusargs("source=%s",source)) $fatal(1,"Pass +source=<tape>");
        tape = $fopen(source,"r");
        if (!tape) $fatal(1,"Could not open pair fixture");
        while (rows < 1024 && !$feof(tape)) load_row();
        $fclose(tape);
        if (rows != 1024) $fatal(1,"Pair regression needs 1024 rows");
        repeat (3) @(negedge clk);
        rst = 0;
        while (emitted < rows) begin
            @(negedge clk);
            if (selected < rows) begin
                s_valid = 1;
                s_request = input_rows[selected];
            end else begin
                s_valid = 0;
            end
            noise = {noise[30:0],noise[31] ^ noise[26] ^ noise[5] ^ noise[0]};
            m_ready = cycles > 100 && noise[2:0] < 5;
            @(posedge clk);
            check_edge();
        end
        if (blocked < 100 || paired < 100 || dispatching_both < 100 || !overlapped)
            $fatal(1,"Pair regression did not cover true overlap and held completions");
        $display("PASS two arithmetic lanes: %0d ordered replies, %0d cycles with two requests, %0d combined transfers",
                 emitted,paired,overlapped);
        $finish;
    end
    initial begin #2000000; $fatal(1,"Ordered pair regression watchdog"); end
endmodule

`default_nettype wire
