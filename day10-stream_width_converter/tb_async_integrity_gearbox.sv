`timescale 1ns/1ps
`default_nettype none

module tb_async_integrity_gearbox #(
    parameter integer IN_BYTES=4, OUT_BYTES=8, MAX_FRAME=64,
    parameter integer SOURCE_HALF=3, DEST_HALF=5,
    parameter integer IN_ADDR_W=2, AUDIT_ADDR_W=1, COUNT_W=4
);
    localparam integer DATA_W=9*OUT_BYTES+17;
    localparam integer RESPONSE_W=180+2*COUNT_W;
    logic source_clk=0,dest_clk=0,power_reset=1;
    always #(SOURCE_HALF) source_clk=!source_clk;
    initial begin
        #1;
        forever #(DEST_HALF) dest_clk=!dest_clk;
    end

    logic s_valid=0,s_last;
    wire s_ready;
    logic [8*IN_BYTES-1:0] s_data;
    logic [IN_BYTES-1:0] s_keep;
    logic [15:0] s_tag,s_expected_bytes;
    logic [31:0] s_expected_crc;
    logic barrier_valid=0;
    wire barrier_ready;
    logic [15:0] barrier_tag;

    wire m_valid,m_last;
    logic m_ready=0;
    wire [8*OUT_BYTES-1:0] m_data;
    wire [OUT_BYTES-1:0] m_keep;
    wire [15:0] m_tag;
    wire audit_valid,audit_barrier;
    logic audit_ready=0;
    wire [15:0] audit_tag,audit_bytes,audit_expected_bytes;
    wire [31:0] audit_crc,audit_expected_crc;
    wire [2:0] audit_status;
    wire [63:0] audit_dest_tick;
    wire [COUNT_W-1:0] audit_retired_frames,audit_forwarded_frames;
    async_integrity_gearbox #(
        .IN_BYTES(IN_BYTES),.OUT_BYTES(OUT_BYTES),.MAX_FRAME(MAX_FRAME),
        .IN_ADDR_W(IN_ADDR_W),.AUDIT_ADDR_W(AUDIT_ADDR_W),.COUNT_W(COUNT_W)
    ) dut (.*);

    wire [DATA_W-1:0] packet = {m_data,m_keep,m_last,m_tag};
    wire [RESPONSE_W-1:0] audit_word = {audit_barrier,audit_tag,audit_bytes,audit_expected_bytes,
        audit_crc,audit_expected_crc,audit_status,audit_dest_tick,audit_retired_frames,audit_forwarded_frames};
    integer dest_file, audit_file;
    integer dest_cycle=0, source_cycle=0, checked_beats=0, checked_frames=0;
    integer checked_audits=0, checked_barriers=0, source_stalls=0, dest_stalls=0, audit_stalls=0;
    logic test_enabled=0;
    logic packet_stalled=0,audit_stalled=0;
    logic [DATA_W-1:0] held_packet;
    logic [RESPONSE_W-1:0] held_audit;
    logic [63:0] last_stamp=0;
    string folder,trace;

    always @(negedge dest_clk) begin
        if (power_reset || !test_enabled)
            m_ready <= 0;
        else
            m_ready <= dest_cycle > 55 && dest_cycle%23 < 16 && dest_cycle%5 != 1;
    end
    always @(negedge source_clk) begin
        if (power_reset || !test_enabled)
            audit_ready <= 0;
        else
            audit_ready <= source_cycle > 90 && source_cycle%53 < 39 && source_cycle%5 != 1;
    end

    always @(posedge dest_clk) begin : check_data
        logic [8*OUT_BYTES-1:0] expected_data;
        logic [OUT_BYTES-1:0] expected_keep;
        logic expected_last;
        logic [15:0] expected_tag;
        if (power_reset || !test_enabled) begin
            packet_stalled <= 0;
            dest_cycle <= 0;
        end else begin
            dest_cycle <= dest_cycle+1;
            if (packet_stalled && (!m_valid || packet !== held_packet))
                $fatal(1,"destination offer changed while held");
            packet_stalled <= m_valid && !m_ready;
            held_packet <= packet;
            dest_stalls <= dest_stalls+(m_valid && !m_ready);
            if (m_valid && m_ready) begin
                if ($fscanf(dest_file,"%h %h %h %h\n",
                            expected_data,expected_keep,expected_last,expected_tag) != 4)
                    $fatal(1,"unexpected destination beat");
                if (packet !== {expected_data,expected_keep,expected_last,expected_tag})
                    $fatal(1,"destination beat mismatch index=%0d",checked_beats);
                checked_beats <= checked_beats+1;
                checked_frames <= checked_frames+m_last;
            end
        end
    end

    always @(posedge source_clk) begin : check_audit
        logic expected_barrier;
        logic [15:0] expected_tag,expected_bytes,declared_bytes;
        logic [31:0] expected_crc,declared_crc;
        logic [2:0] expected_status;
        logic [COUNT_W-1:0] expected_retired,expected_passed;
        integer completed_before_barrier;
        if (power_reset || !test_enabled) begin
            audit_stalled <= 0;
            source_cycle <= 0;
        end else begin
            source_cycle <= source_cycle+1;
            source_stalls <= source_stalls+(s_valid && !s_ready);
            audit_stalls <= audit_stalls+(audit_valid && !audit_ready);
            if (audit_stalled && (!audit_valid || audit_word !== held_audit))
                $fatal(1,"returned report changed while held");
            audit_stalled <= audit_valid && !audit_ready;
            held_audit <= audit_word;
            if (audit_valid && audit_ready) begin
                if ($fscanf(audit_file,"%h %h %h %h %h %h %h %h %h %h\n",
                            expected_barrier,expected_tag,expected_bytes,declared_bytes,expected_crc,
                            declared_crc,expected_status,expected_retired,expected_passed,
                            completed_before_barrier) != 10)
                    $fatal(1,"unexpected returned report");
                if ({audit_barrier,audit_tag,audit_bytes,audit_expected_bytes,audit_crc,
                     audit_expected_crc,audit_status,audit_retired_frames,audit_forwarded_frames} !==
                    {expected_barrier,expected_tag,expected_bytes,declared_bytes,expected_crc,
                     declared_crc,expected_status,expected_retired,expected_passed})
                    $fatal(1,"return tuple mismatch at report %0d",checked_audits);
                if (checked_audits != 0 && audit_dest_tick <= last_stamp)
                    $fatal(1,"destination timestamps re-ordered or tied");
                if (audit_barrier && checked_frames < completed_before_barrier)
                    $fatal(1,"barrier acknowledged before earlier final beats");
                last_stamp <= audit_dest_tick;
                checked_audits <= checked_audits+1;
                checked_barriers <= checked_barriers+audit_barrier;
            end
        end
    end

    initial begin
        integer commands, totals, read_row, stop_reports, stop_beats, stop_frames, stop_barriers;
        logic [1:0] kind;
        logic [8*IN_BYTES-1:0] data;
        logic [IN_BYTES-1:0] keep;
        logic last;
        logic [15:0] tag,bytes_declared;
        logic [31:0] checksum;
        if (!$value$plusargs("vectors=%s",folder))
            $fatal(1,"missing async gearbox tape");
        if ($value$plusargs("wave=%s",trace)) begin
            $dumpfile(trace);
            $dumpvars(0,tb_async_integrity_gearbox);
        end
        commands=$fopen({folder,"/commands.txt"},"r");
        dest_file=$fopen({folder,"/packets.txt"},"r");
        audit_file=$fopen({folder,"/reports.txt"},"r");
        totals=$fopen({folder,"/totals.txt"},"r");
        if (!commands || !dest_file || !audit_file || !totals)
            $fatal(1,"cannot open async gearbox inputs");
        if ($fscanf(totals,"%h %h %h %h\n",stop_reports,stop_beats,stop_frames,stop_barriers) != 4)
            $fatal(1,"invalid fixture totals");
        $fclose(totals);

        repeat (6) @(negedge source_clk);
        power_reset=0;
        // This packet and barrier are intentionally abandoned under a common
        // reset while both destination packet and source report are held.
        @(negedge source_clk);
        {s_valid,s_data,s_keep,s_last,s_tag,s_expected_bytes,s_expected_crc} =
            {1'b1,(8*IN_BYTES)'(8'h41),IN_BYTES'(1),1'b1,16'hdead,16'd1,32'hd3d99e8b};
        @(posedge source_clk);
        while (!s_ready) @(posedge source_clk);
        @(negedge source_clk);
        s_valid=0;
        barrier_valid=1;
        barrier_tag=16'hfeed;
        @(posedge source_clk);
        while (!barrier_ready) @(posedge source_clk);
        @(negedge source_clk);
        barrier_valid=0;
        wait (m_valid && audit_valid);
        repeat (4) @(negedge dest_clk);
        if (!m_valid || !audit_valid || audit_barrier || audit_tag != 16'hdead)
            $fatal(1,"could not stage both reset victims");
        #1;
        power_reset=1;
        #1;
        if (m_valid || audit_valid || s_ready || barrier_ready)
            $fatal(1,"transfer visible while the shared reset is asserted");
        repeat (6) @(negedge source_clk);
        repeat (6) @(negedge dest_clk);
        #1;
        power_reset=0;
        repeat (8) @(negedge source_clk);
        if (m_valid || audit_valid)
            $fatal(1,"old outputs escaped the reset epoch");
        test_enabled=1;

        read_row=$fscanf(commands,"%h %h %h %h %h %h %h\n",kind,data,keep,last,tag,bytes_declared,checksum);
        while (read_row==7) begin
            case (kind)
                0: begin
                    {s_valid,s_data,s_keep,s_last,s_tag,s_expected_bytes,s_expected_crc} =
                        {1'b1,data,keep,last,tag,bytes_declared,checksum};
                    @(posedge source_clk);
                    while (!s_ready) @(posedge source_clk);
                    @(negedge source_clk);
                    s_valid=0;
                end
                1: begin
                    barrier_valid=1;
                    barrier_tag=tag;
                    @(posedge source_clk);
                    while (!barrier_ready) @(posedge source_clk);
                    @(negedge source_clk);
                    barrier_valid=0;
                end
                2: begin
                    barrier_valid=1;
                    barrier_tag=tag;
                    @(posedge source_clk);
                    if (barrier_ready) $fatal(1,"barrier accepted in the middle of an input frame");
                    @(negedge source_clk);
                end
                default: $fatal(1,"invalid CDC command marker");
            endcase
            read_row=$fscanf(commands,"%h %h %h %h %h %h %h\n",kind,data,keep,last,tag,bytes_declared,checksum);
        end
        if (read_row != -1 || barrier_valid || s_valid) $fatal(1,"bad input tape ending");

        wait (checked_beats == stop_beats && checked_audits == stop_reports);
        repeat (30) @(negedge source_clk);
        if (checked_frames != stop_frames || checked_barriers != stop_barriers ||
            audit_valid || m_valid || !source_stalls || !dest_stalls || !audit_stalls)
            $fatal(1,"scoreboard conservation or CDC stress failed");
        $display("PASS CDC %0d:%0d clocks=%0d/%0d frames=%0d beats=%0d reports=%0d barriers=%0d held=%0d/%0d/%0d",
                 IN_BYTES,OUT_BYTES,SOURCE_HALF,DEST_HALF,checked_frames,checked_beats,checked_audits,
                 checked_barriers,source_stalls,dest_stalls,audit_stalls);
        $finish;
    end

    initial begin
        #3000000;
        $fatal(1,"CDC gearbox timeout after %0d reports and %0d beats",checked_audits,checked_beats);
    end
endmodule

`default_nettype wire
