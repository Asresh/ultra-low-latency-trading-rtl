`timescale 1ns/1ps
`default_nettype none

module tb_spliced_packet_gearbox #(
    parameter integer IN_BYTES=8, OUT_BYTES=5, MAX_FRAME=128, MAX_PREFIX=8
);
    localparam integer PREFIX_W=$clog2(MAX_PREFIX+1);
    localparam integer DATA_W=9*OUT_BYTES+18;
    localparam integer REPORT_W=51;
    logic clk=0,rst=1;
    always #5 clk=!clk;

    logic edit_valid=0,edit_crc;
    wire edit_ready;
    logic [15:0] edit_tag,edit_length,edit_trim_head,edit_trim_tail,edit_patch_offset;
    logic [PREFIX_W-1:0] edit_prefix_length;
    logic [8*MAX_PREFIX-1:0] edit_prefix;
    logic [3:0] edit_patch_keep;
    logic [31:0] edit_patch_bytes;
    logic s_valid=0,s_first,s_last;
    wire s_ready;
    logic [8*IN_BYTES-1:0] s_data;
    logic [IN_BYTES-1:0] s_keep;
    logic m_ready=0,report_ready=0;
    wire m_valid,m_first,m_last,report_valid;
    wire [8*OUT_BYTES-1:0] m_data;
    wire [OUT_BYTES-1:0] m_keep;
    wire [15:0] m_tag,report_tag,report_input_bytes,report_output_bytes;
    wire [2:0] report_status;

    spliced_packet_gearbox #(
        .IN_BYTES(IN_BYTES),.OUT_BYTES(OUT_BYTES),.MAX_FRAME(MAX_FRAME),.MAX_PREFIX(MAX_PREFIX)
    ) dut (.*);

    wire [DATA_W-1:0] converted={m_data,m_keep,m_first,m_last,m_tag};
    wire [REPORT_W-1:0] report={report_tag,report_input_bytes,report_output_bytes,report_status};
    logic data_held=0,audit_held=0;
    logic [DATA_W-1:0] old_data;
    logic [REPORT_W-1:0] old_audit;
    integer index_cycle=0,actual_data=0,actual_reports=0,output_frames=0,report_stalls=0,data_stalls=0;
    integer descriptor_stalls=0;
    integer outcomes [0:5];
    integer out_fd,report_fd;
    string folder,wave;

    always @(negedge clk) begin
        m_ready <= !rst && index_cycle>=32 && index_cycle%31<23 && index_cycle%5!=0;
        report_ready <= !rst && index_cycle>=18 && index_cycle%37<32 && index_cycle%5!=1;
    end

    always @(posedge clk) begin : check_streams
        logic [8*OUT_BYTES-1:0] expected_data;
        logic [OUT_BYTES-1:0] expected_keep;
        logic expected_first,expected_last;
        logic [15:0] expected_tag,expected_length,expected_output;
        logic [2:0] expected_status;
        if (!rst) begin
            index_cycle<=index_cycle+1;
            if(data_held && (!m_valid || converted!==old_data))
                $fatal(1,"splice output changed while held");
            if(audit_held && (!report_valid || report!==old_audit))
                $fatal(1,"splice audit changed while held");
            data_held<=m_valid && !m_ready;
            audit_held<=report_valid && !report_ready;
            old_data<=converted;
            old_audit<=report;
            data_stalls<=data_stalls+(m_valid && !m_ready);
            report_stalls<=report_stalls+(report_valid && !report_ready);
            descriptor_stalls<=descriptor_stalls+(edit_valid && !edit_ready);
            if(m_valid && m_ready) begin
                if($fscanf(out_fd,"%h %h %h %h %h\n",
                           expected_data,expected_keep,expected_first,expected_last,expected_tag)!=5)
                    $fatal(1,"unexpected edited packet beat");
                if(converted!=={expected_data,expected_keep,expected_first,expected_last,expected_tag})
                    $fatal(1,"byte exact splice mismatch beat=%0d got=%h",actual_data,converted);
                actual_data<=actual_data+1;
                output_frames<=output_frames+m_last;
            end
            if(report_valid && report_ready) begin
                if($fscanf(report_fd,"%h %h %h %h\n",
                           expected_tag,expected_length,expected_output,expected_status)!=4)
                    $fatal(1,"unexpected splice verdict");
                if(report!=={expected_tag,expected_length,expected_output,expected_status})
                    $fatal(1,"splice audit mismatch index=%0d got=%x expected status=%d",
                           actual_reports,report,expected_status);
                actual_reports<=actual_reports+1;
                outcomes[expected_status]<=outcomes[expected_status]+1;
            end
        end
    end

    initial begin
        integer drive_fd,total_fd,read_row,beat_count,end_data,end_reports;
        if(!$value$plusargs("vectors=%s",folder)) $fatal(1,"missing splice vectors");
        if($value$plusargs("wave=%s",wave)) begin
            $dumpfile(wave);
            $dumpvars(0,tb_spliced_packet_gearbox);
        end
        drive_fd=$fopen({folder,"/commands.txt"},"r");
        out_fd=$fopen({folder,"/packets.txt"},"r");
        report_fd=$fopen({folder,"/reports.txt"},"r");
        total_fd=$fopen({folder,"/totals.txt"},"r");
        if(!drive_fd || !out_fd || !report_fd || !total_fd ||
           $fscanf(total_fd,"%h %h\n",end_data,end_reports)!=2)
            $fatal(1,"could not open splice files");
        for(integer status=0;status<6;status=status+1) outcomes[status]=0;
        repeat(3) @(negedge clk);
        rst=0;
        read_row=$fscanf(drive_fd,"%h %h %h %h %h %h %h %h %h %h %h\n",
            edit_tag,edit_length,edit_trim_head,edit_trim_tail,edit_prefix_length,edit_prefix,
            edit_patch_offset,edit_patch_keep,edit_patch_bytes,edit_crc,beat_count);
        while(read_row==11) begin
            edit_valid=1;
            @(posedge clk);
            while(!edit_ready) @(posedge clk);
            @(negedge clk);
            edit_valid=0;
            for(integer beat=0;beat<beat_count;beat=beat+1) begin
                if($fscanf(drive_fd,"%h %h %h %h\n",s_data,s_keep,s_first,s_last)!=4)
                    $fatal(1,"bad ingress beat row");
                s_valid=1;
                @(posedge clk);
                while(!s_ready) @(posedge clk);
                @(negedge clk);
                s_valid=0;
            end
            read_row=$fscanf(drive_fd,"%h %h %h %h %h %h %h %h %h %h %h\n",
                edit_tag,edit_length,edit_trim_head,edit_trim_tail,edit_prefix_length,edit_prefix,
                edit_patch_offset,edit_patch_keep,edit_patch_bytes,edit_crc,beat_count);
        end
        if(read_row!=-1) $fatal(1,"bad descriptor row");
        wait(actual_data==end_data && actual_reports==end_reports);
        repeat(20) @(negedge clk);
        if(m_valid || report_valid || output_frames!=outcomes[0] ||
           data_stalls==0 || report_stalls==0 || descriptor_stalls==0)
            $fatal(1,"splice scenario did not retire or hit expected backpressure");
        for(integer status=0;status<6;status=status+1)
            if(!outcomes[status]) $fatal(1,"missing splice verdict %0d",status);
        $display("PASS splice %0d:%0d cap=%0d verdicts=%0d frames=%0d beats=%0d held=%0d/%0d/%0d",
                 IN_BYTES,OUT_BYTES,MAX_FRAME,actual_reports,output_frames,actual_data,
                 descriptor_stalls,data_stalls,report_stalls);
        $finish;
    end

    initial begin
        #2000000;
        $fatal(1,"splice fixture timed out");
    end
endmodule

`default_nettype wire
