`timescale 1ns/1ps
`default_nettype none

module tb_timestamped_stream_replayer #(
    parameter integer DEPTH = 16
);
    localparam integer CW = $clog2(DEPTH+1);
    logic clk = 0;
    always #5 clk = !clk;

    logic rst = 1, cfg_valid = 0, rsp_ready = 0, m_ready = 0;
    logic [2:0] cfg_op = 0;
    logic [31:0] cfg_tick = 0, cfg_gap = 0;
    logic [63:0] cfg_data = 0;
    logic [7:0] cfg_keep = 0;
    logic cfg_last = 0;
    logic [15:0] cfg_tag = 0, cfg_repeats = 0;

    wire cfg_ready, rsp_valid, m_valid, m_last, active, paused, done;
    wire [3:0] rsp_status;
    wire [CW-1:0] rsp_length;
    wire [63:0] m_data;
    wire [7:0] m_keep;
    wire [15:0] m_tag, m_pass;
    wire [47:0] m_due, m_offered, now_tick;
    wire [31:0] emitted_beats, emitted_packets, late_beats, completed_passes;

    timestamped_stream_replayer #(.DEPTH(DEPTH)) dut (.*);

    // A transcript records both held and accepted output pins before each rising edge.
    // These pins, including the clock and counters, are verified by the Python model.
    integer drive_file, trace_file, expected_rows;
    string stimulus_path, transcript_path;
    logic [200:0] last_offer_bits;
    logic [CW+3:0] last_result_bits;
    logic last_offer_stalled = 0, last_result_stalled = 0;

    // Read one fixed-format set of inputs after the falling edge and settle the DUT.
    task automatic drive_row(input integer number);
        integer read_fields;
        begin
            @(negedge clk);
            read_fields = $fscanf(
                drive_file, "%h %h %h %h %h %h %h %h %h %h %h %h\n",
                rst,cfg_valid,cfg_op,cfg_tick,cfg_data,cfg_keep,cfg_last,cfg_tag,
                cfg_repeats,cfg_gap,rsp_ready,m_ready
            );
            if (read_fields != 12)
                $fatal(1,"malformed stimulus at cycle %0d, fields=%0d",number,read_fields);
            #1;
        end
    endtask

    // Assert stability of every valid output field through a stall and independent pause.
    task automatic check_guards;
        logic [200:0] current_offer_bits;
        logic [CW+3:0] current_result_bits;
        begin
            current_offer_bits = {m_pass,m_offered,m_due,m_tag,m_last,m_keep,m_data};
            current_result_bits = {rsp_status,rsp_length};
            if (!rst) begin
                if (last_offer_stalled && (m_valid !== 1 || last_offer_bits !== current_offer_bits))
                    $fatal(1,"replay output changed while backpressured");
                if (last_result_stalled && (rsp_valid !== 1 || last_result_bits !== current_result_bits))
                    $fatal(1,"configuration response changed while backpressured");
                if (m_valid && m_offered < m_due)
                    $fatal(1,"an event became visible before its due tick");
                if (paused && !active)
                    $fatal(1,"the device is paused without an active run");
                if (done && active)
                    $fatal(1,"the device announced done without leaving the run");
            end
            last_offer_stalled = m_valid && !m_ready && !rst;
            last_result_stalled = rsp_valid && !rsp_ready && !rst;
            last_offer_bits = current_offer_bits;
            last_result_bits = current_result_bits;
        end
    endtask

    // Write the canonical pin sample. Idle channel payload words are ignored by the oracle.
    task automatic record_row(input integer number);
        begin
            $fwrite(
                trace_file, "%0d %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h\n",
                number,cfg_ready,rsp_valid,rsp_status,rsp_length,m_valid,m_data,m_keep,
                m_last,m_tag,m_due,m_offered,m_pass,active,paused,done,now_tick,
                emitted_beats,emitted_packets,late_beats,completed_passes
            );
        end
    endtask

    initial begin
        if (!$value$plusargs("stim=%s",stimulus_path))
            $fatal(1,"missing +stim argument");
        if (!$value$plusargs("trace=%s",transcript_path))
            $fatal(1,"missing +trace argument");
        drive_file = $fopen(stimulus_path,"r");
        trace_file = $fopen(transcript_path,"w");
        if (!drive_file || !trace_file)
            $fatal(1,"unable to open replay fixture files");
        if ($fscanf(drive_file,"%d\n",expected_rows) != 1 || expected_rows <= 0)
            $fatal(1,"invalid stimulus length");
        @(posedge clk);
        for (integer index = 0; index < expected_rows; index++) begin
            drive_row(index);
            check_guards();
            record_row(index);
            @(posedge clk);
        end
        $fclose(drive_file);
        $fclose(trace_file);
        $display("PASS replay pin capture, depth=%0d rows=%0d",DEPTH,expected_rows);
        $finish;
    end
endmodule

`default_nettype wire
