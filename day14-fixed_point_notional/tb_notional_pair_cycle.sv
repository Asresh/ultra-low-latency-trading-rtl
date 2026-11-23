`timescale 1ns/1ps
`default_nettype none

// External cycle driver. The Python audit compares the visible reply, both
// handshake directions, and the pre-edge outstanding count on every edge.
module tb_notional_pair_cycle #(
    parameter integer PRICE_FRAC = 8
);
    logic clk = 0;
    logic rst = 1;
    logic s_valid = 0;
    logic s_ready;
    logic [163:0] s_request = '0;
    logic m_valid;
    logic m_ready = 0;
    logic [212:0] m_reply;
    logic [1:0] outstanding;

    logic raw_reset;
    logic raw_valid;
    logic raw_ready;
    logic [15:0] raw_tag;
    logic [31:0] raw_price;
    logic [31:0] raw_qty;
    logic [15:0] raw_fee;
    logic [1:0] raw_round;
    logic raw_side;
    logic raw_limited;
    logic [63:0] raw_limit;

    logic [15:0] result_tag;
    logic [2:0] result_status;
    logic result_side;
    logic [63:0] result_gross;
    logic [63:0] result_fee;
    logic [63:0] result_cash;
    logic result_inexact;

    integer drive_file;
    integer captured;
    integer scanned;
    integer cycle;
    integer requests;
    integer replies;
    integer reset_cycles;
    string drive_path;
    string capture_path;

    notional_pair #(.PRICE_FRAC(PRICE_FRAC)) dut (.*);
    assign {result_tag, result_status, result_side, result_gross,
            result_fee, result_cash, result_inexact} = m_reply;

    // Sample normalized pins after combinational signals settle.
    task automatic sample_outputs;
        if ($isunknown(s_ready) || $isunknown(m_valid) || $isunknown(outstanding))
            $fatal(1, "unknown output control at cycle %0d", cycle);
        if (!m_valid && m_reply !== 213'b0)
            $fatal(1, "inactive response was not zero at cycle %0d", cycle);
        if (m_valid && $isunknown(m_reply))
            $fatal(1, "unknown payload on a valid result at cycle %0d", cycle);
        $fdisplay(
            captured, "%0h %0h %0h %0h %0h %0h %0h %0h %0h %0h",
            s_ready, m_valid, outstanding, result_tag, result_status, result_side,
            result_gross, result_fee, result_cash, result_inexact
        );
    endtask

    // Apply a source row, sample both interfaces, and commit the clock edge.
    task automatic apply_cycle;
        rst = raw_reset;
        s_valid = raw_valid;
        m_ready = raw_ready;
        s_request = {
            raw_tag, raw_price, raw_qty, raw_fee, raw_round,
            raw_side, raw_limited, raw_limit
        };
        #4;
        sample_outputs();
        if (s_ready && s_valid)
            requests = requests + 1;
        if (m_ready && m_valid)
            replies = replies + 1;
        if (rst)
            reset_cycles = reset_cycles + 1;
        clk = 1;
        #1;
        clk = 0;
        cycle = cycle + 1;
    endtask

    initial begin
        if (!$value$plusargs("drive=%s", drive_path))
            $fatal(1, "provide +drive=<cycle tape>");
        if (!$value$plusargs("capture=%s", capture_path))
            $fatal(1, "provide +capture=<pin trace>");
        drive_file = $fopen(drive_path, "r");
        captured = $fopen(capture_path, "w");
        if (!drive_file || !captured)
            $fatal(1, "could not open the pair drive tape and pin trace");

        cycle = 0;
        requests = 0;
        replies = 0;
        reset_cycles = 0;
        // A known reset before the first sample avoids power-on X values.
        #1;
        clk = 1;
        #1;
        clk = 0;
        while (!$feof(drive_file)) begin
            scanned = $fscanf(
                drive_file, "%h %h %h %h %h %h %h %h %h %h %h",
                raw_reset, raw_valid, raw_ready, raw_tag, raw_price, raw_qty,
                raw_fee, raw_round, raw_side, raw_limited, raw_limit
            );
            if (scanned == 11)
                apply_cycle();
            else if (scanned > 0 || !$feof(drive_file))
                $fatal(1, "drive row %0d has %0d columns", cycle, scanned);
        end
        if (cycle == 0 || outstanding != 0 || m_valid)
            $fatal(1, "pair schedule did not end in an observable empty state");
        $fclose(drive_file);
        $fclose(captured);
        $display("q%0d sampled %0d edges, accepted %0d, delivered %0d, reset cycles %0d",
                 PRICE_FRAC, cycle, requests, replies, reset_cycles);
        $finish;
    end
endmodule

`default_nettype wire
