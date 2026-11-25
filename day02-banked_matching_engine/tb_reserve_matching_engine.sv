`timescale 1ns/1ps
`default_nettype none

// Read the stimulus and record both ready/valid channels before each edge.
module tb_reserve_matching_engine #(
    parameter integer BANKS = 3,
    parameter integer PRICE_LEVELS = 12,
    parameter integer ORDER_SLOTS = 12,
    parameter integer QTY_W = 16,
    parameter integer OWNER_W = 8,
    parameter integer TAG_W = 16,
    parameter integer SEQ_W = 8,
    parameter integer CMD_DEPTH = 4,
    parameter integer EVENT_DEPTH = 4
);
    localparam integer BW = $clog2(BANKS + 1);
    localparam integer PW = $clog2(PRICE_LEVELS + 1);
    localparam integer IW = $clog2(ORDER_SLOTS + 1);
    localparam integer SW = QTY_W + IW;
    localparam integer CW = 8 + IW + PW + 3*QTY_W + OWNER_W;
    localparam integer RW = 11 + IW + PW + 5*QTY_W + OWNER_W + 2*SW + IW;
    localparam integer RUN_LIMIT = 150000;

    logic clk = 1'b0;
    always #5 clk = !clk;
    logic rst = 1'b1, s_valid = 1'b0, m_ready = 1'b0;
    logic [BW-1:0] s_bank = '0;
    logic [TAG_W-1:0] s_tag = '0;
    logic [CW-1:0] s_command = '0;
    wire s_ready, m_valid;
    wire [BW-1:0] m_bank;
    wire [TAG_W-1:0] m_tag;
    wire [SEQ_W-1:0] m_command_sequence, m_event_sequence;
    wire [IW-1:0] m_order_id;
    wire [RW-1:0] m_result;
    wire [3:0] m_kind = m_result[RW-1-:4];
    wire m_last = m_result[RW-5];
    wire [IW-1:0] m_maker = m_result[RW-7-:IW];
    wire [QTY_W-1:0] m_quantity = m_result[RW-7-IW-PW-:QTY_W];
    wire m_refresh = m_result[4+IW+2*SW];

    reserve_matching_engine #(
        .BANKS(BANKS), .PRICE_LEVELS(PRICE_LEVELS), .ORDER_SLOTS(ORDER_SLOTS),
        .QTY_W(QTY_W), .OWNER_W(OWNER_W), .TAG_W(TAG_W), .SEQ_W(SEQ_W),
        .CMD_DEPTH(CMD_DEPTH), .EVENT_DEPTH(EVENT_DEPTH)
    ) dut (.*);

    integer inputs, trace, action, fields;
    integer cycles = 0, hold_cycles = 0, idle_cycles = 2, tail_cycles = 0;
    logic end_of_tape = 1'b0, pending = 1'b0;
    logic [31:0] noise = 32'he3021ac7;
    string input_path, trace_path, vcd_path;

    always @(posedge clk) begin
        $fdisplay(trace, "%0d %b %b %b %b %b %h %h %h %h %h %h %h %h %h %h",
            cycles, rst, s_valid, s_ready, m_valid, m_ready, s_bank, s_tag, s_command,
            m_bank, m_tag, m_command_sequence, m_event_sequence, m_order_id,
            m_result, dut.lane_valid);
        cycles <= cycles + 1;
    end

    initial begin
        if (!$value$plusargs("tape=%s",input_path) || !$value$plusargs("trace=%s",trace_path))
            $fatal(1,"require +tape and +trace");
        if ($value$plusargs("waveform=%s",vcd_path)) begin
            $dumpfile(vcd_path);
            $dumpvars(1,tb_reserve_matching_engine);
        end
        inputs = $fopen(input_path,"r");
        trace = $fopen(trace_path,"w");
        if (!inputs || !trace) $fatal(1,"file open failed");
        repeat (3) @(negedge clk);
        while (tail_cycles < 1024) begin
            rst = 1'b0;
            if (!pending && !end_of_tape && idle_cycles == 0 && (|noise[4:3])) begin
                fields = $fscanf(inputs,"%d %h %h %h",action,s_bank,s_tag,s_command);
                if (fields == 4) begin
                    case (action)
                        0: pending = 1'b1;
                        1: idle_cycles = int'(s_command);
                        2: begin
                            rst = 1'b1;
                            hold_cycles = 0;
                            idle_cycles = 2;
                        end
                        3: hold_cycles = int'(s_command);
                        default: $fatal(1,"unexpected stimulus action %0d",action);
                    endcase
                end else if ($feof(inputs))
                    end_of_tape = 1'b1;
                else
                    $fatal(1,"malformed stimulus row");
            end
            s_valid = pending;
            m_ready = !rst && (hold_cycles == 0) && (|noise[2:0]);
            noise = {noise[30:0], noise[31] ^ noise[28] ^ noise[5] ^ noise[0]};
            if (hold_cycles > 0) hold_cycles = hold_cycles - 1;
            if (idle_cycles > 0) idle_cycles = idle_cycles - 1;
            @(posedge clk);
            if (pending && s_ready) pending = 1'b0;
            if (end_of_tape) tail_cycles = tail_cycles + 1;
            if (cycles > RUN_LIMIT) $fatal(1,"testbench watchdog reached with input outstanding");
            @(negedge clk);
        end
        $fclose(inputs);
        $fclose(trace);
        $display("Reserve matcher captured %0d cycles",cycles);
        $finish;
    end
endmodule

`default_nettype wire
