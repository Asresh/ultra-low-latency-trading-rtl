`timescale 1ns/1ps
`default_nettype none

// Feed the bench one independent cycle-model row per clock. Compare every
// valid/ready signal and active payload before any synchronous state update.
module tb_credit_order_scheduler #(
    parameter integer CLASSES = 3,
    parameter integer DEPTH = 4,
    parameter integer CREDIT_W = 8
);
    localparam integer CLASS_W = (CLASSES > 1) ? $clog2(CLASSES) : 1;
    localparam integer COUNT_W = $clog2(DEPTH + 1);

    logic clk = 0, rst = 1;
    logic s_valid = 0;
    wire s_ready, s_rejected;
    logic [CLASS_W-1:0] s_class = 0;
    logic [CREDIT_W-1:0] s_cost = 0;
    logic [15:0] s_tag = 0;
    logic [31:0] s_payload = 0;

    wire m_valid;
    logic m_ready = 0;
    wire [CLASS_W-1:0] m_class;
    wire [CREDIT_W-1:0] m_cost;
    wire [15:0] m_tag;
    wire [31:0] m_payload;

    logic ctl_valid = 0;
    wire ctl_ready;
    logic [1:0] ctl_op = 0;
    logic [CLASS_W-1:0] ctl_class = 0;
    logic [CREDIT_W-1:0] ctl_amount = 0;
    logic [3:0] ctl_weight = 0;

    wire report_valid;
    logic report_ready = 0;
    wire [1:0] report_op;
    wire [CLASS_W-1:0] report_class;
    wire [2:0] report_status;
    wire [CREDIT_W-1:0] report_capacity, report_available, report_inflight;
    wire [COUNT_W-1:0] report_queued;
    wire [3:0] report_weight;

    credit_order_scheduler #(.CLASSES(CLASSES), .DEPTH(DEPTH), .CREDIT_W(CREDIT_W)) dut (.*);

    always #5 clk = !clk;

    // Keep independent hold checks in the bench so the stability contract is
    // visible even if somebody changes the software's arbitration.
    localparam integer OFFER_W = CLASS_W + CREDIT_W + 48;
    localparam integer REPORT_W = 2 + CLASS_W + 3 + 3*CREDIT_W + COUNT_W + 4;
    wire [OFFER_W-1:0] offer_bits = {m_class, m_cost, m_tag, m_payload};
    wire [REPORT_W-1:0] report_bits = {report_op, report_class, report_status,
                                     report_capacity, report_available, report_inflight,
                                     report_queued, report_weight};
    logic [OFFER_W-1:0] previous_offer;
    logic [REPORT_W-1:0] previous_report;
    bit hold_offer, hold_report;
    integer accepted = 0, sent = 0, rejected = 0, reports = 0;

    // Drive one row and return its parsed field count to the file loop.
    task automatic read_drive(input integer source, output integer fields);
        fields = $fscanf(source, "%h %h %h %h %h %h %h %h %h %h %h %h %h\n",
                         rst, s_valid, s_class, s_cost, s_tag, s_payload, m_ready,
                         ctl_valid, ctl_op, ctl_class, ctl_amount, ctl_weight, report_ready);
    endtask

    // Report the precise failing pin and input row when a comparison differs.
    task automatic same(input integer row, input string name,
                        input logic [31:0] observed, input logic [31:0] expected);
        if (observed !== expected)
            $fatal(1, "cycle %0d %s: got %h expected %h", row, name, observed, expected);
    endtask

    // Read and compare a pin row. A channel's payload is ignored only when
    // its valid bit has separately matched zero.
    task automatic compare_row(input integer golden, input integer row);
        logic [31:0] want [0:16];
        integer fields;
        fields = $fscanf(golden, "%h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h\n",
                         want[0], want[1], want[2], want[3], want[4], want[5], want[6],
                         want[7], want[8], want[9], want[10], want[11], want[12],
                         want[13], want[14], want[15], want[16]);
        if (fields != 17)
            $fatal(1, "expected stream ended or malformed at cycle %0d (fields %0d)", row, fields);
        same(row, "s_ready", 32'(s_ready), want[0]);
        same(row, "s_rejected", 32'(s_rejected), want[1]);
        same(row, "ctl_ready", 32'(ctl_ready), want[2]);
        same(row, "m_valid", 32'(m_valid), want[3]);
        if (m_valid) begin
            same(row, "m_class", 32'(m_class), want[4]);
            same(row, "m_cost", 32'(m_cost), want[5]);
            same(row, "m_tag", 32'(m_tag), want[6]);
            same(row, "m_payload", m_payload, want[7]);
        end
        same(row, "report_valid", 32'(report_valid), want[8]);
        if (report_valid) begin
            same(row, "report_op", 32'(report_op), want[9]);
            same(row, "report_class", 32'(report_class), want[10]);
            same(row, "report_status", 32'(report_status), want[11]);
            same(row, "report_capacity", 32'(report_capacity), want[12]);
            same(row, "report_available", 32'(report_available), want[13]);
            same(row, "report_inflight", 32'(report_inflight), want[14]);
            same(row, "report_queued", 32'(report_queued), want[15]);
            same(row, "report_weight", 32'(report_weight), want[16]);
        end
    endtask

    // Check hold-until-accepted and count actual (rather than modeled) transfers.
    task automatic check_handshakes(input integer row);
        if (!rst && hold_offer && (!m_valid || offer_bits !== previous_offer))
            $fatal(1, "cycle %0d changed a stalled order offer", row);
        if (!rst && hold_report && (!report_valid || report_bits !== previous_report))
            $fatal(1, "cycle %0d overwrote a stalled report", row);
        hold_offer = m_valid && !m_ready;
        hold_report = report_valid && !report_ready;
        previous_offer = offer_bits;
        previous_report = report_bits;
        if (s_valid && s_ready && !s_rejected) accepted++;
        if (s_rejected) rejected++;
        if (report_valid && report_ready) reports++;
        if (m_valid && m_ready) sent++;
    endtask

    initial begin : run
        string directory, wave;
        integer drive_file, golden, fields, row, excess;
        if (!$value$plusargs("vectors=%s", directory))
            $fatal(1, "missing +vectors=DIR");
        if ($value$plusargs("wave=%s", wave)) begin
            $dumpfile(wave);
            $dumpvars(2, tb_credit_order_scheduler);
        end
        drive_file = $fopen({directory, "/drive.hex"}, "r");
        golden = $fopen({directory, "/expected.hex"}, "r");
        if (!drive_file || !golden)
            $fatal(1, "could not open drive and expected vectors in %s", directory);

        row = 0;
        #2;
        read_drive(drive_file, fields);
        while (fields == 13) begin
            #1;
            compare_row(golden, row);
            check_handshakes(row);
            row++;
            @(negedge clk);
            #2;
            read_drive(drive_file, fields);
        end
        if (!$feof(drive_file) || (fields != -1 && fields != 0) || row == 0)
            $fatal(1, "bad drive row %0d (fields %0d)", row, fields);
        if ($fscanf(golden, "%h", excess) == 1 || !$feof(golden))
            $fatal(1, "expected stream contains surplus data");
        $fclose(drive_file);
        $fclose(golden);
        $display("PASS %0d checked cycles, %0d enqueued, %0d sent, %0d rejected, %0d reports",
                 row, accepted, sent, rejected, reports);
        $finish;
    end
endmodule

`default_nettype wire
