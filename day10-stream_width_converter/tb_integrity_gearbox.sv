`timescale 1ns/1ps
`default_nettype none

module tb_integrity_gearbox #(
    parameter integer IN_BYTES = 4,
    parameter integer OUT_BYTES = 8,
    parameter integer MAX_FRAME = 128
);
    localparam integer PACKET_W = OUT_BYTES*9 + 17;
    localparam integer AUDIT_W = 16*3 + 32*2 + 3;
    logic clk = 0;
    always #5 clk = !clk;

    logic rst = 1, s_valid = 0, s_last, m_ready = 0, audit_ready = 0;
    logic [8*IN_BYTES-1:0] s_data;
    logic [IN_BYTES-1:0] s_keep;
    logic [15:0] s_tag, s_expected_bytes;
    logic [31:0] s_expected_crc;
    wire s_ready, m_valid, m_last, audit_valid;
    wire [8*OUT_BYTES-1:0] m_data;
    wire [OUT_BYTES-1:0] m_keep;
    wire [15:0] m_tag, audit_tag, audit_bytes, audit_expected_bytes;
    wire [31:0] audit_crc, audit_expected_crc;
    wire [2:0] audit_status;
    wire [PACKET_W-1:0] packet = {m_data,m_keep,m_last,m_tag};
    wire [AUDIT_W-1:0] audit = {audit_tag,audit_bytes,audit_expected_bytes,
                               audit_crc,audit_expected_crc,audit_status};

    integrity_gearbox #(.IN_BYTES(IN_BYTES), .OUT_BYTES(OUT_BYTES), .MAX_FRAME(MAX_FRAME))
        dut (.*);

    integer input_file, output_file, audit_file, rc;
    integer cycle = 0, consumed = 0, reports = 0, beats = 0, frames = 0;
    integer packet_stalls = 0, audit_stalls = 0, idle = 0;
    integer statuses [0:5];
    logic check_stream = 1;
    logic packet_held = 0, audit_held = 0, accepted = 0, input_done = 0;
    logic [PACKET_W-1:0] previous_packet;
    logic [AUDIT_W-1:0] previous_audit;
    logic [31:0] ready_rng = 32'h1367ace0;
    logic [OUT_BYTES*8-1:0] expected_data;
    logic [OUT_BYTES-1:0] expected_keep;
    logic expected_last;
    logic [15:0] expected_tag, expected_byte_count, expected_declaration;
    logic [31:0] expected_crc, declared_crc;
    logic [2:0] expected_status;
    string directory, vcd;

    task automatic hold_single_byte;
        s_data = (IN_BYTES*8)'(8'h41);
        s_keep = IN_BYTES'(1);
        s_expected_bytes = 1;
        s_expected_crc = 32'hd3d99e8b;
        s_tag = 16'h5aa5;
        s_valid = 1;
        s_last = 1;
        @(posedge clk);
        if (!s_ready) $fatal(1, "one-byte test input did not accept");
        @(negedge clk);
        s_valid = 0;
        while (!audit_valid || !m_valid) @(negedge clk);
        if (audit_status != 0 || audit_tag != 16'h5aa5 || audit_crc != 32'hd3d99e8b)
            $fatal(1, "bad one-byte audit");
    endtask

    initial begin
        if (!$value$plusargs("tape=%s", directory))
            $fatal(1,"missing tape directory");
        if ($value$plusargs("wave=%s", vcd)) begin
            $dumpfile(vcd);
            $dumpvars(0, tb_integrity_gearbox);
        end
        input_file = $fopen({directory,"/input.txt"},"r");
        output_file = $fopen({directory,"/packets.txt"},"r");
        audit_file = $fopen({directory,"/audit.txt"},"r");
        if (!input_file || !output_file || !audit_file)
            $fatal(1,"could not open integrity vectors");
        for (integer index = 0; index < 6; index = index+1)
            statuses[index] = 0;
        repeat (2) @(negedge clk);
        rst = 0;
        while (idle < 48) begin
            if ((!s_valid || accepted) && !input_done) begin
                rc = $fscanf(input_file,"%h %h %h %h %h %h\n",
                             s_data,s_keep,s_last,s_tag,s_expected_bytes,s_expected_crc);
                if (rc == -1) begin
                    input_done = 1;
                    s_valid = 0;
                end else if (rc != 6) begin
                    $fatal(1,"bad input tape row");
                end else begin
                    s_valid = 1;
                end
            end
            ready_rng = {ready_rng[30:0],ready_rng[31]^ready_rng[21]^ready_rng[1]^ready_rng[0]};
            m_ready = input_done || (cycle >= 26 && cycle%43 < 32 && ready_rng[5]);
            audit_ready = input_done || (cycle >= 12 && cycle%37 < 28 && ready_rng[7]);
            @(negedge clk);
        end
        rc = $fscanf(output_file,"%h",expected_data);
        if (rc != -1) $fatal(1,"output tape not drained");
        rc = $fscanf(audit_file,"%h",expected_tag);
        if (rc != -1) $fatal(1,"audit tape not drained");
        check_stream = 0;
        m_ready = 0;
        audit_ready = 0;
        hold_single_byte();
        // A ready consumer must see no transfer on the reset edge.
        rst = 1;
        audit_ready = 1;
        m_ready = 1;
        #1;
        if (audit_valid || m_valid || s_ready)
            $fatal(1, "reset allowed a stale transfer");
        @(negedge clk);
        rst = 0;
        audit_ready = 0;
        m_ready = 0;
        #1;
        if (audit_valid || m_valid) $fatal(1, "reset retained committed output");
        hold_single_byte();
        $display("PASS integrity %0d:%0d cap=%0d input=%0d reports=%0d frames=%0d beats=%0d stalls=%0d/%0d statuses=%0d,%0d,%0d,%0d,%0d,%0d",
                 IN_BYTES,OUT_BYTES,MAX_FRAME,consumed,reports,frames,beats,packet_stalls,audit_stalls,
                 statuses[0],statuses[1],statuses[2],statuses[3],statuses[4],statuses[5]);
        $finish;
    end

    initial begin
        #800000;
        $fatal(1, "integrity test timeout");
    end

    always @(posedge clk) begin
        if (!rst && check_stream) begin
            cycle <= cycle + 1;
            if (cycle >= 60000) $fatal(1,"integrity tape timeout");
            accepted <= s_valid && s_ready;
            consumed <= consumed + (s_valid && s_ready);
            if (m_valid || audit_valid || !input_done)
                idle <= 0;
            else
                idle <= idle + 1;
            if (packet_held && (!m_valid || packet !== previous_packet))
                $fatal(1,"held packet changed");
            if (audit_held && (!audit_valid || audit !== previous_audit))
                $fatal(1,"held audit changed");
            if (!m_valid && packet !== '0)
                $fatal(1,"failed integrity leaked output payload");
            packet_held <= m_valid && !m_ready;
            audit_held <= audit_valid && !audit_ready;
            previous_packet <= packet;
            previous_audit <= audit;
            packet_stalls <= packet_stalls + (m_valid && !m_ready);
            audit_stalls <= audit_stalls + (audit_valid && !audit_ready);
            if (m_valid && m_ready) begin
                if ($fscanf(output_file,"%h %h %h %h\n",
                            expected_data,expected_keep,expected_last,expected_tag) != 4)
                    $fatal(1,"unexpected output beat at cycle %0d",cycle);
                if (packet !== {expected_data,expected_keep,expected_last,expected_tag})
                    $fatal(1,"packet mismatch cycle=%0d expected=%h got=%h", cycle,
                           {expected_data,expected_keep,expected_last,expected_tag},packet);
                beats <= beats + 1;
                frames <= frames + m_last;
            end
            if (audit_valid && audit_ready) begin
                if ($fscanf(audit_file,"%h %h %h %h %h %h\n", expected_tag,expected_byte_count,
                            expected_declaration,expected_crc,declared_crc,expected_status) != 6)
                    $fatal(1,"unexpected audit at cycle %0d",cycle);
                if (audit !== {expected_tag,expected_byte_count,expected_declaration,expected_crc,
                               declared_crc,expected_status})
                    $fatal(1,"audit mismatch cycle=%0d expected=%h got=%h",cycle,
                           {expected_tag,expected_byte_count,expected_declaration,expected_crc,
                            declared_crc,expected_status},audit);
                reports <= reports + 1;
                statuses[expected_status] <= statuses[expected_status] + 1;
            end
        end
    end
endmodule

`default_nettype wire
