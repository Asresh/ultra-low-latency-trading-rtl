`timescale 1ns/1ps
`default_nettype none

module tb_banked_call_auction #(
    parameter integer BANKS=3, PRICES=16, SLOTS=16, QTY_W=16, OWNER_W=8, EPOCH_W=16,
                      TAG_W=16, SEQ_W=16, CLOCK_W=48, CMD_DEPTH=4, EVENT_DEPTH=4
);
    localparam integer BW=$clog2(BANKS+1), PW=$clog2(PRICES+1), IW=$clog2(SLOTS+1), SW=QTY_W+IW;
    localparam integer CW=7+EPOCH_W+TAG_W+IW+OWNER_W+3*PW+QTY_W;
    localparam integer RW=13+EPOCH_W+TAG_W+5*IW+2*OWNER_W+PW+3*SW;
    localparam integer OW=BW+2*SEQ_W+3+RW;
    logic clk=0,rst=1;
    logic s_valid=0,m_ready=0,ctl_valid=0,ctl_report_ready=0;
    wire s_ready,m_valid,ctl_ready,ctl_report_valid,m_scheduled;
    logic [BW-1:0] s_bank=0;
    wire [BW-1:0] m_bank;
    logic [CW-1:0] s_command=0;
    wire [RW-1:0] m_result;
    wire [SEQ_W-1:0] m_command_sequence,m_event_sequence;
    wire [1:0] m_wrapper_status,ctl_report_op,ctl_report_status;
    logic [1:0] ctl_op=0;
    logic [BANKS-1:0] ctl_mask=0;
    logic [BANKS*EPOCH_W-1:0] ctl_epochs=0;
    logic [CLOCK_W-1:0] ctl_cutoff=0;
    logic [TAG_W-1:0] ctl_tag=0;
    wire [TAG_W-1:0] ctl_report_tag;
    wire [BANKS-1:0] ctl_report_mask,ctl_report_armed,schedule_armed,schedule_due,schedule_queued;
    wire [BANKS*3-1:0] ctl_report_detail;
    wire [BANKS*CLOCK_W-1:0] ctl_report_cutoffs;
    wire [CLOCK_W-1:0] clock_tick;
    wire [OW-1:0] response = {m_bank,m_command_sequence,m_event_sequence,m_scheduled,m_wrapper_status,m_result};
    wire [3:0] event_kind = m_result[RW-1-:4];
    wire [TAG_W-1:0] event_tag = m_result[RW-10-EPOCH_W-:TAG_W];
    wire event_last = m_result[RW-9];

    banked_call_auction #(
        .BANKS(BANKS),.PRICES(PRICES),.SLOTS(SLOTS),.QTY_W(QTY_W),.OWNER_W(OWNER_W),
        .EPOCH_W(EPOCH_W),.TAG_W(TAG_W),.SEQ_W(SEQ_W),.CLOCK_W(CLOCK_W),.CMD_DEPTH(CMD_DEPTH),
        .EVENT_DEPTH(EVENT_DEPTH)
    ) dut (.*);

    integer tape,trace,fields,cycle=0;
    string source,destination,wave;
    initial begin
        if (!$value$plusargs("input=%s",source) || !$value$plusargs("output=%s",destination))
            $fatal(1,"supply banked-auction input and output paths");
        tape=$fopen(source,"r");
        trace=$fopen(destination,"w");
        if (!tape || !trace) $fatal(1,"unable to open artifacts");
        if ($value$plusargs("wave=%s",wave)) begin
            $dumpfile(wave);
            $dumpvars(1,tb_banked_call_auction);
        end
        #5 clk=1;
        #5 clk=0;
        while (!$feof(tape)) begin
            fields=$fscanf(tape,"%h %h %h %h %h %h %h %h %h %h %h %h\n",
                rst,s_valid,s_bank,s_command,ctl_valid,ctl_op,ctl_mask,ctl_epochs,ctl_cutoff,ctl_tag,
                m_ready,ctl_report_ready);
            if (fields != 12) $fatal(1,"bad stimulus at %0d",cycle);
            #5;
            $fdisplay(trace,"%0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h %0h",
                s_ready,ctl_ready,m_valid,m_valid ? response : '0,ctl_report_valid,
                ctl_report_valid ? ctl_report_op : 0,ctl_report_valid ? ctl_report_status : 0,
                ctl_report_valid ? ctl_report_mask : 0,ctl_report_valid ? ctl_report_tag : 0,
                ctl_report_valid ? ctl_report_detail : 0,ctl_report_valid ? ctl_report_armed : 0,
                ctl_report_valid ? ctl_report_cutoffs : 0,clock_tick,schedule_armed,schedule_due,
                schedule_queued,dut.lane_valid);
            clk=1;
            #5 clk=0;
            cycle++;
        end
        $fclose(tape);
        $fclose(trace);
        $display("Captured %0d banked auction cycles across %0d instruments",cycle,BANKS);
        $finish;
    end
endmodule

`default_nettype wire
