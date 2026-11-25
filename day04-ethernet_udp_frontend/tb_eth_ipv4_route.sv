`timescale 1ns/1ps
`default_nettype none

module tb_eth_ipv4_route #(parameter integer ROWS = 8, localparam integer ROW_W = $clog2(ROWS+1));
    logic clk = 0;
    always #5 clk = !clk;
    logic rst = 1;
    logic s_valid, s_tagged, m_ready, cfg_valid, cfg_enabled, cfg_tagged, cfg_reject, commit_valid;
    logic [11:0] s_vid, cfg_vid;
    logic [31:0] s_destination, cfg_network, cfg_gateway;
    logic [15:0] s_cookie;
    logic [ROW_W-1:0] cfg_row;
    logic [5:0] cfg_prefix;
    wire s_ready, m_valid, m_tagged, cfg_ready, cfg_error, commit_ready;
    wire [1:0] m_status;
    wire [15:0] m_cookie;
    wire [7:0] m_epoch, active_epoch;
    wire [11:0] m_vid;
    wire [31:0] m_next_hop;
    wire [ROW_W-1:0] m_row;
    eth_ipv4_route #(.ROWS(ROWS), .EPOCH_W(8)) dut (.*);

    logic want_s_ready, want_cfg_ready, want_commit, want_error, want_valid;
    logic [7:0] want_epoch, want_lookup_epoch;
    logic [1:0] want_status;
    logic [15:0] want_cookie;
    logic want_tagged;
    logic [11:0] want_vid;
    logic [31:0] want_hop;
    logic [ROW_W-1:0] want_row;
    integer tape, fields, sampled = 0, transfers = 0, stalls = 0, errors = 0;
    string path;

    // Compare pre-edge readiness and the registered result in separate slots.
    initial begin
        if (!$value$plusargs("tape=%s", path))
            $fatal(1, "Specify +tape=FILE");
        tape = $fopen(path,"r");
        if (!tape) $fatal(1, "Could not read IPv4 route tape");
        s_valid = 0;
        cfg_valid = 0;
        commit_valid = 0;
        m_ready = 0;
        @(posedge clk);
        while (!$feof(tape)) begin
            @(negedge clk);
            fields = $fscanf(tape,
                "%h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h\n",
                rst,s_valid,s_tagged,s_vid,s_destination,s_cookie,
                cfg_valid,cfg_row,cfg_enabled,cfg_tagged,cfg_vid,cfg_network,cfg_prefix,
                cfg_reject,cfg_gateway,commit_valid,m_ready,
                want_s_ready,want_cfg_ready,want_commit,want_error,want_epoch,
                want_valid,want_status,want_cookie,want_lookup_epoch,want_tagged,want_vid,want_hop,want_row);
            if (fields != 30)
                $fatal(1, "Wrong number of fields at sample %0d: %0d",sampled,fields);
            #1;
            if ({s_ready,cfg_ready,(commit_valid && commit_ready)} !==
                {want_s_ready,want_cfg_ready,want_commit})
                $fatal(1, "Input handshakes disagree at cycle %0d",sampled);
            if (!rst && m_valid && m_ready) transfers++;
            if (!rst && m_valid && !m_ready) stalls++;
            @(posedge clk);
            #1;
            if ({active_epoch,cfg_error,m_valid} !== {want_epoch,want_error,want_valid})
                $fatal(1, "Config status or result valid mismatch at cycle %0d",sampled);
            if (cfg_error) errors++;
            if (m_valid && {m_status,m_cookie,m_epoch,m_tagged,m_vid,m_next_hop,m_row} !==
                {want_status,want_cookie,want_lookup_epoch,want_tagged,want_vid,want_hop,want_row})
                $fatal(1, "Cycle %0d expected status/hop %0d/%h, observed %0d/%h",
                       sampled,want_status,want_hop,m_status,m_next_hop);
            sampled++;
        end
        if (transfers < 500 || stalls < 100 || errors < 40)
            $fatal(1, "Incomplete model check: %0d replies, %0d holds, %0d invalid writes",
                   transfers,stalls,errors);
        $display("PASS IPv4 LPM rows=%0d: %0d cycles, %0d replies, %0d holds, %0d invalid writes",
                 ROWS,sampled,transfers,stalls,errors);
        $fclose(tape);
        $finish;
    end
    initial begin #100000; $fatal(1,"route test timeout"); end
endmodule

`default_nettype wire
