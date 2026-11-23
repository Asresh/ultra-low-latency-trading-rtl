`timescale 1ns/1ps
`default_nettype none

module tb_gearbox_faults;
    logic clk = 0;
    always #5 clk = ~clk;
    logic rst = 1;
    logic s_valid = 0, s_last = 0, m_ready = 0, report_ready = 0;
    logic [63:0] s_data = '0;
    logic [7:0] s_keep = '0;
    logic [15:0] s_tag = 16'hE9A3;
    wire s_ready, m_valid, m_last, report_valid;
    wire [31:0] m_data;
    wire [3:0] m_keep;
    wire [15:0] m_tag, report_tag, report_bytes;
    wire [2:0] report_status;
    wire [31:0] accepted_packets, rejected_packets, emitted_packets, emitted_bytes;
    integer valid_masks = 0, invalid_masks = 0, reported_bytes = 0;
    integer data_size, lane_digits, horizon;

    packet_gearbox #(.IN_BYTES(8),.OUT_BYTES(4),.MAX_FRAME(32)) dut (.*);

    // Write one beat under ready/valid and leave both payload fields held until it retires.
    task automatic beat(input logic [63:0] word_value, input logic [7:0] mask,
                        input logic final_beat, input logic [15:0] id);
        @(negedge clk);
        s_valid = 1;
        s_data = word_value;
        s_keep = mask;
        s_last = final_beat;
        s_tag = id;
        do @(posedge clk); while (!s_ready);
        @(negedge clk);
        s_valid = 0;
    endtask

    // Check an independent report and prove it holds while its peer is blocked.
    task automatic acknowledge(input logic [2:0] status, input logic [15:0] length,
                               input logic [15:0] id, input integer waits);
        if (!report_valid) $fatal(1,"Missing report after last input");
        repeat (waits) begin
            if (!report_valid || {report_status,report_bytes,report_tag} !== {status,length,id})
                $fatal(1,"Held report diverged from expected");
            @(negedge clk);
        end
        if (!report_valid || {report_status,report_bytes,report_tag} !== {status,length,id})
            $fatal(1,"Report mismatch got status=%0d length=%0d expect=%0d/%0d",
                   report_status,report_bytes,status,length);
        report_ready = 1;
        @(negedge clk);
        report_ready = 0;
        if (report_valid) $fatal(1,"Report transferred more than once");
    endtask

    // Match each emitted byte to a constant proof pattern, ignoring poison input padding.
    task automatic drain_known_pattern(input integer length, input logic [15:0] id);
        integer cursor, fragment;
        logic [31:0] word_value;
        logic [3:0] lanes;
        cursor = 0;
        while (cursor < length) begin
            if (!m_valid) $fatal(1,"Validated packet lost an output beat");
            fragment = length-cursor < 4 ? length-cursor : 4;
            word_value = '0;
            lanes = '0;
            for (integer lane = 0; lane < fragment; lane++) begin
                lanes[lane] = 1;
                word_value[8*lane+:8] = 8'(8'h31 + cursor + lane);
            end
            if ({m_tag,m_data,m_keep,m_last} !== {id,word_value,lanes,(cursor+fragment == length)})
                $fatal(1,"Final-mask packet mismatch length %0d cursor %0d",length,cursor);
            m_ready = 1;
            @(negedge clk);
            m_ready = 0;
            cursor += fragment;
        end
        if (m_valid) $fatal(1,"Packet duplicated after final output");
    endtask

    initial begin
        repeat (3) @(negedge clk);
        rst = 0;

        // Classify the entire eight-lane mask space against independent bit positions.
        for (integer pattern = 0; pattern < 256; pattern++) begin
            lane_digits = 0;
            horizon = -1;
            for (integer bit_no = 0; bit_no < 8; bit_no++) begin
                if ((pattern >> bit_no) & 1) begin
                    lane_digits++;
                    horizon = bit_no;
                end
            end
            beat(64'h3837363534333231, 8'(pattern), 1, 16'(pattern));
            if (lane_digits != 0 && lane_digits == horizon + 1) begin
                valid_masks++;
                data_size = lane_digits;
                acknowledge(0, 16'(data_size), 16'(pattern), pattern % 3);
                drain_known_pattern(data_size,16'(pattern));
                reported_bytes += data_size;
            end else begin
                invalid_masks++;
                acknowledge(1,16'(lane_digits),16'(pattern),pattern % 2);
                if (m_valid) $fatal(1,"Gapped mask released input bytes");
            end
        end

        // Once the byte limit is exceeded, continue draining to a real last.
        // The report's 16-bit count saturates even though no output was committed.
        for (integer count = 0; count < 8201; count++)
            beat(64'hABAB121201234567, 8'hFF, 0, 16'h8312);
        if (m_valid || report_valid) $fatal(1,"Unterminated frame released result");
        beat(64'h55, 8'h01, 1, 16'h886F);
        acknowledge(3,16'hFFFF,16'h8312,15);
        if (m_valid) $fatal(1,"Oversized packet reached downstream");

        // A reset while two good packets are queued flushes both banks and counters.
        beat(64'h31,8'h01,1,16'h21);
        acknowledge(0,1,16'h21,0);
        beat(64'h3231,8'h03,1,16'h22);
        acknowledge(0,2,16'h22,0);
        if (!m_valid || s_ready) $fatal(1,"Both banks must be occupied");
        rst = 1;
        @(negedge clk);
        rst = 0;
        @(negedge clk);
        if (m_valid || report_valid || !s_ready ||
            {accepted_packets,rejected_packets,emitted_packets,emitted_bytes} != 0)
            $fatal(1,"Reset leaked queue state");
        beat(64'h333231,8'h07,1,16'h23);
        acknowledge(0,3,16'h23,0);
        drain_known_pattern(3,16'h23);
        if ({accepted_packets,rejected_packets,emitted_packets,emitted_bytes} !==
            {32'd1,32'd0,32'd1,32'd3})
            $fatal(1,"Fresh counters do not match post-reset traffic");
        if (valid_masks != 8 || invalid_masks != 248 || reported_bytes != 36)
            $fatal(1,"Mask fixture did not reach all expected cases");
        $display("PASS %0d valid/%0d rejected masks, saturated length after 65609 bytes, two-bank reset",
                 valid_masks,invalid_masks);
        $finish;
    end
    initial begin #4500000; $fatal(1,"Malformed frame watchdog"); end
endmodule

`default_nettype wire
