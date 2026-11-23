`timescale 1ns/1ps
`default_nettype none

// One independent driver and scoreboard for a direction of the full top.
module width_lane_agent #(
    parameter integer IN_BYTES = 4,
    parameter integer OUT_BYTES = 8,
    parameter integer SEED = 29,
    parameter integer FRAMES = 190
) (
    input wire clk,
    input wire rst,
    output logic valid,
    input wire ready,
    output logic [IN_BYTES*8-1:0] data,
    output logic [IN_BYTES-1:0] keep,
    output logic last,
    output logic [15:0] tag,
    output logic sink_ready,
    input wire sink_valid,
    input wire [OUT_BYTES*8-1:0] sink_data,
    input wire [OUT_BYTES-1:0] sink_keep,
    input wire sink_last,
    input wire [15:0] sink_tag,
    output logic report_ready,
    input wire report_valid,
    input wire [15:0] report_tag,
    input wire [15:0] report_bytes,
    input wire [2:0] report_status,
    input wire [31:0] accepted,
    input wire [31:0] rejected,
    input wire [31:0] emitted,
    input wire [31:0] byte_counter,
    output logic done
);
    integer frame_no, source_offset, sent_frames;
    integer report_cursor, sink_cursor, sink_offset, sink_frames, sink_bytes;
    integer reports_good, reports_bad;
    integer source_stalls, sink_stalls, report_stalls;
    integer head_cycle, egress_cycle;
    integer lat_min, lat_max;
    logic take_last_seen;
    logic [31:0] lfsr;
    logic source_held, sink_held, report_held;
    logic [9*OUT_BYTES+16:0] seen_sink;
    logic [34:0] seen_report;
    logic [9*IN_BYTES+16:0] seen_source;
    integer completed_at [0:FRAMES-1];

    // Vary the input length across lane residues and the frame capacity.
    function automatic integer length_at(input integer frame);
        length_at = 1 + ((frame * 17 + SEED) % 128);
        if (frame % 17 == 4)
            length_at = 130;
    endfunction

    // Keep source identity independent in the two directions, with intentional reuse.
    function automatic logic [15:0] id_at(input integer frame);
        id_at = 16'(SEED + (frame % 23) * 71);
    endfunction

    // Select difficult bytes, including repeated patterns and bit flips across lanes.
    function automatic logic [7:0] payload_byte(input integer frame, input integer offset);
        integer scrambled;
        begin
            scrambled = (offset + SEED * 11) ^ (frame * 87);
            payload_byte = 8'((scrambled * 117) ^ (offset << 3) ^ (scrambled >> 4));
        end
    endfunction

    // Mark tag faults independently of length failures.
    function automatic logic crooked_tag(input integer frame);
        crooked_tag = frame % 9 == 2 && length_at(frame) > IN_BYTES;
    endfunction

    // Independently classify the rules used by this directed fixture.
    function automatic logic [2:0] expected_code(input integer frame);
        if (crooked_tag(frame) && (frame % 8 != 3 || length_at(frame) > 2*IN_BYTES))
            expected_code = 2;
        else if (frame % 8 == 3)
            expected_code = 1;
        else if (length_at(frame) > 128)
            expected_code = 3;
        else
            expected_code = 0;
    endfunction

    // A final zero mask contributes zero selected bytes even though last is present.
    function automatic integer diagnostic_size(input integer frame);
        integer len;
        begin
            len = length_at(frame);
            if (frame % 8 == 3)
                diagnostic_size = ((len - 1) / IN_BYTES) * IN_BYTES;
            else
                diagnostic_size = len;
        end
    endfunction

    // Drive a complete beat using packet bytes that are a pure function of its offset.
    task automatic put_beat;
        integer remaining;
        remaining = length_at(frame_no) - source_offset;
        data = '0;
        keep = '0;
        tag = id_at(frame_no);
        if (crooked_tag(frame_no) && source_offset == IN_BYTES) tag ^= 16'h1234;
        last = remaining <= IN_BYTES;
        for (integer lane = 0; lane < IN_BYTES; lane++) begin
            data[8*lane+:8] = lane < remaining ? payload_byte(frame_no,source_offset+lane) : 8'hDD;
            keep[lane] = lane < remaining;
        end
        if (last && frame_no % 8 == 3) keep = '0;
    endtask

    // Compare each active sink lane directly to the generator, without repacking an input beat.
    task automatic check_sink;
        integer remaining, valid_bytes;
        logic [OUT_BYTES-1:0] expected_mask;
        logic [OUT_BYTES*8-1:0] expected_data;
        while (sink_cursor < sent_frames && expected_code(sink_cursor) != 0)
            sink_cursor++;
        if (sink_cursor >= sent_frames)
            $fatal(1,"Width lane %0d emitted data without a complete accepted packet",SEED);
        remaining = length_at(sink_cursor) - sink_offset;
        valid_bytes = remaining < OUT_BYTES ? remaining : OUT_BYTES;
        expected_mask = '0;
        expected_data = '0;
        for (integer lane = 0; lane < OUT_BYTES; lane++) begin
            if (lane < valid_bytes) begin
                expected_mask[lane] = 1;
                expected_data[8*lane+:8] = payload_byte(sink_cursor,sink_offset+lane);
            end
        end
        if ({sink_tag,sink_last,sink_keep,sink_data} !==
            {id_at(sink_cursor),(valid_bytes == remaining),expected_mask,expected_data})
            $fatal(1,"Width lane %0d payload mismatch frame=%0d offset=%0d",SEED,sink_cursor,sink_offset);
        if (sink_offset == 0) begin
            head_cycle = egress_cycle - completed_at[sink_cursor];
            if (head_cycle < lat_min) lat_min = head_cycle;
            if (head_cycle > lat_max) lat_max = head_cycle;
        end
        sink_bytes += valid_bytes;
        if (sink_last) begin
            sink_cursor++;
            sink_offset = 0;
            sink_frames++;
        end else begin
            sink_offset += valid_bytes;
        end
    endtask

    // Reports independently account for failed frames, byte count, and first fault.
    task automatic check_report;
        if (report_cursor == sent_frames)
            $fatal(1,"Width lane %0d produced a report without accepted last",SEED);
        if ({report_tag,report_bytes,report_status} !==
            {id_at(report_cursor),16'(diagnostic_size(report_cursor)),expected_code(report_cursor)})
            $fatal(1,"Width lane %0d wrong report for frame %0d: tag=%h bytes=%0d status=%d",
                   SEED,report_cursor,report_tag,report_bytes,report_status);
        if (report_status == 0)
            reports_good++;
        else
            reports_bad++;
        report_cursor++;
    endtask

    // Carry output and report holds across clock edges and verify them separately.
    task automatic check_holds;
        if (source_held && (!valid || {data,keep,last,tag} !== seen_source))
            $fatal(1,"Fixture mutates a pending source beat");
        if (sink_held && (!sink_valid || {sink_data,sink_keep,sink_last,sink_tag} !== seen_sink))
            $fatal(1,"Width lane %0d output changed under stall",SEED);
        if (report_held && (!report_valid || {report_tag,report_bytes,report_status} !== seen_report))
            $fatal(1,"Width lane %0d report changed under stall",SEED);
        source_held = valid && !ready;
        sink_held = sink_valid && !sink_ready;
        report_held = report_valid && !report_ready;
        seen_source = {data,keep,last,tag};
        seen_sink = {sink_data,sink_keep,sink_last,sink_tag};
        seen_report = {report_tag,report_bytes,report_status};
        source_stalls += int'(source_held);
        sink_stalls += int'(sink_held);
        report_stalls += int'(report_held);
    endtask

    always @(negedge clk) begin
        if (rst) begin
            lfsr = 32'h8bc47256 ^ 32'(SEED);
            valid = 0;
            data = '0;
            keep = '0;
            last = 0;
            tag = '0;
            sink_ready = 0;
            report_ready = 0;
        end else begin
            lfsr = {lfsr[30:0],lfsr[31]^lfsr[28]^lfsr[7]^lfsr[0]};
            sink_ready = egress_cycle > 80 && lfsr[3:0] < 12;
            report_ready = egress_cycle > 35 && lfsr[12:8] < 25;
            if (!source_held) begin
                valid = frame_no < FRAMES && lfsr[18:15] != 0;
                if (valid) put_beat();
            end
        end
    end

    always @(posedge clk) begin
        if (rst) begin
            frame_no = 0;
            source_offset = 0;
            sent_frames = 0;
            report_cursor = 0;
            sink_cursor = 0;
            sink_offset = 0;
            egress_cycle = 0;
            reports_good = 0;
            reports_bad = 0;
            sink_frames = 0;
            sink_bytes = 0;
            source_held = 0;
            sink_held = 0;
            report_held = 0;
            source_stalls = 0;
            sink_stalls = 0;
            report_stalls = 0;
            lat_min = 32767;
            lat_max = 0;
            done = 0;
        end else if (!done) begin
            egress_cycle++;
            check_holds();
            if (sink_valid && sink_ready) check_sink();
            if (report_valid && report_ready) check_report();
            if (valid && ready) begin
                if (last) begin
                    completed_at[frame_no] = egress_cycle;
                    sent_frames++;
                    frame_no++;
                    source_offset = 0;
                end else begin
                    source_offset += IN_BYTES;
                end
            end
            if (report_cursor == FRAMES && reports_good == sink_frames && !sink_valid) begin
                if ({accepted,rejected,emitted,byte_counter} !==
                    {32'(reports_good),32'(reports_bad),32'(sink_frames),32'(sink_bytes)})
                    $fatal(1,"Width lane %0d end counters differ",SEED);
                if (source_stalls == 0 || sink_stalls == 0 || report_stalls == 0)
                    $fatal(1,"Width lane %0d does not cover all stall types",SEED);
                $display("PASS full top lane %0d->%0d %0d good/%0d bad %0d bytes commit-to-output %0d..%0d cycles",
                         IN_BYTES,OUT_BYTES,reports_good,reports_bad,sink_bytes,lat_min,lat_max);
                done = 1;
            end
        end
    end
endmodule

module tb_stream_width_converter;
    logic clk = 0;
    always #5 clk = ~clk;
    logic rst = 1;
    wire s32_valid, s32_ready, s32_last, m64_valid, m64_ready, m64_last;
    wire s64_valid, s64_ready, s64_last, m32_valid, m32_ready, m32_last;
    wire [31:0] s32_data, m32_data;
    wire [63:0] s64_data, m64_data;
    wire [3:0] s32_keep, m32_keep;
    wire [7:0] s64_keep, m64_keep;
    wire [15:0] s32_tag, m32_tag, s64_tag, m64_tag;
    wire up_report_valid, up_report_ready, down_report_valid, down_report_ready;
    wire [15:0] up_report_tag, up_report_bytes, down_report_tag, down_report_bytes;
    wire [2:0] up_report_status, down_report_status;
    wire [31:0] up_accepted, up_rejected, up_emitted, up_bytes;
    wire [31:0] down_accepted, down_rejected, down_emitted, down_bytes;
    wire up_done, down_done;
    integer simultaneous = 0;

    stream_width_converter dut (.*);
    width_lane_agent #(.IN_BYTES(4),.OUT_BYTES(8),.SEED(3)) up (
        .clk,.rst,.valid(s32_valid),.ready(s32_ready),.data(s32_data),.keep(s32_keep),
        .last(s32_last),.tag(s32_tag),.sink_ready(m64_ready),.sink_valid(m64_valid),
        .sink_data(m64_data),.sink_keep(m64_keep),.sink_last(m64_last),.sink_tag(m64_tag),
        .report_ready(up_report_ready),.report_valid(up_report_valid),.report_tag(up_report_tag),
        .report_bytes(up_report_bytes),.report_status(up_report_status),.accepted(up_accepted),
        .rejected(up_rejected),.emitted(up_emitted),.byte_counter(up_bytes),.done(up_done)
    );
    width_lane_agent #(.IN_BYTES(8),.OUT_BYTES(4),.SEED(123)) down (
        .clk,.rst,.valid(s64_valid),.ready(s64_ready),.data(s64_data),.keep(s64_keep),
        .last(s64_last),.tag(s64_tag),.sink_ready(m32_ready),.sink_valid(m32_valid),
        .sink_data(m32_data),.sink_keep(m32_keep),.sink_last(m32_last),.sink_tag(m32_tag),
        .report_ready(down_report_ready),.report_valid(down_report_valid),.report_tag(down_report_tag),
        .report_bytes(down_report_bytes),.report_status(down_report_status),.accepted(down_accepted),
        .rejected(down_rejected),.emitted(down_emitted),.byte_counter(down_bytes),.done(down_done)
    );

    always @(posedge clk)
        if (m64_valid && m64_ready && m32_valid && m32_ready)
            simultaneous++;

    initial begin
        repeat (5) @(negedge clk);
        rst = 0;
        wait (up_done && down_done);
        if (simultaneous < 100) $fatal(1,"Both directions must overlap");
        $display("PASS full top: %0d simultaneous output-transfer edges",simultaneous);
        $finish;
    end
    initial begin #1000000; $fatal(1,"Full top watchdog"); end
endmodule

`default_nettype wire
