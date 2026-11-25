`timescale 1ns/1ps
`default_nettype none

// Normalize unaligned byte lanes, validate a complete packet, then apply a
// descriptor's strip, prefix and overwrite edits during width conversion.
module spliced_packet_gearbox #(
    parameter integer IN_BYTES=8,
    parameter integer OUT_BYTES=5,
    parameter integer MAX_FRAME=128,
    parameter integer MAX_PREFIX=8,
    localparam integer PREFIX_W=$clog2(MAX_PREFIX+1)
) (
    input wire clk,
    input wire rst,
    input wire edit_valid,
    output wire edit_ready,
    input wire [15:0] edit_tag,
    input wire [15:0] edit_length,
    input wire [15:0] edit_trim_head, edit_trim_tail,
    input wire [PREFIX_W-1:0] edit_prefix_length,
    input wire [8*MAX_PREFIX-1:0] edit_prefix,
    input wire [15:0] edit_patch_offset,
    input wire [3:0] edit_patch_keep,
    input wire [31:0] edit_patch_bytes,
    input wire edit_crc,

    input wire s_valid,
    output wire s_ready,
    input wire [8*IN_BYTES-1:0] s_data,
    input wire [IN_BYTES-1:0] s_keep,
    input wire s_first,s_last,

    output wire m_valid,
    input wire m_ready,
    output logic [8*OUT_BYTES-1:0] m_data,
    output logic [OUT_BYTES-1:0] m_keep,
    output wire m_first,m_last,
    output wire [15:0] m_tag,

    output wire report_valid,
    input wire report_ready,
    output logic [15:0] report_tag,report_input_bytes,report_output_bytes,
    output logic [2:0] report_status
);
    localparam logic [2:0] OK=0, FRAMING=1, LENGTH=2, TOO_LONG=3, BAD_EDIT=4, BAD_PATCH=5;
    localparam logic [31:0] CRC_SEED=32'hffffffff;
    logic [7:0] mem [0:2*MAX_FRAME-1];
    logic [1:0] committed, with_crc;
    logic writer, reader;
    logic [15:0] bank_tag [0:1], keep_from [0:1], body_length [0:1], total_length [0:1];
    logic [PREFIX_W-1:0] prefix_length [0:1];
    logic [8*MAX_PREFIX-1:0] prefix_bytes [0:1];
    logic [15:0] patch_from [0:1];
    logic [3:0] patch_mask [0:1];
    logic [31:0] patch_value [0:1];
    logic collecting, have_first, pending_report;
    logic [15:0] declared, measured;
    logic [2:0] sticky;
    logic [15:0] send_at;
    logic [31:0] crc_state, crc_after;
    logic [15:0] first_lane, lanes;
    logic [IN_BYTES-1:0] normalized_keep;
    logic framing_ok;
    logic [2:0] beat_fault, finish;
    logic invalid_patch;
    wire [16:0] trim_count={1'b0,edit_trim_head}+{1'b0,edit_trim_tail};
    wire [16:0] kept_count={1'b0,edit_length}-trim_count;
    wire [16:0] body_count=kept_count+17'(edit_prefix_length);
    wire [16:0] wire_count=body_count+(edit_crc ? 17'd4 : 17'd0);
    wire invalid_edit=(edit_length==0 || trim_count>{1'b0,edit_length} ||
                       int'(edit_prefix_length)>MAX_PREFIX || wire_count==0 || wire_count[16]);
    wire [16:0] measured_after={1'b0,measured}+{1'b0,lanes};
    wire [15:0] observed=measured_after[16] ? 16'hffff : measured_after[15:0];
    wire capture_descriptor=edit_valid && edit_ready;
    wire capture_beat=s_valid && s_ready;
    wire retire_beat=m_valid && m_ready;

    assign report_valid=!rst && pending_report;
    assign edit_ready=!rst && !collecting && !committed[writer] && (!pending_report || report_ready);
    assign s_ready=!rst && collecting;
    assign m_valid=!rst && committed[reader];
    assign m_tag=m_valid ? bank_tag[reader] : '0;
    assign m_first=m_valid && send_at==0;
    assign m_last=m_valid && {1'b0,send_at}+17'(OUT_BYTES)>={1'b0,total_length[reader]};

    always @* begin : get_width
        first_lane=0;
        lanes=0;
        for(integer i=0;i<IN_BYTES;i=i+1)
            if(s_keep[i]) begin
                if(lanes==0) first_lane=16'(i);
                lanes=lanes+1'b1;
            end
        normalized_keep=s_keep>>first_lane;
        framing_ok=s_first==!have_first && lanes!=0 &&
                   (normalized_keep & (normalized_keep+1'b1))==0 &&
                   (!have_first || first_lane==0) &&
                   (s_last || first_lane+lanes==16'(IN_BYTES));
        beat_fault=sticky;
        if(sticky==OK) begin
            if(!framing_ok)
                beat_fault=FRAMING;
            else if(measured_after>17'(MAX_FRAME))
                beat_fault=TOO_LONG;
        end
        finish=(beat_fault==OK && measured_after!={1'b0,declared}) ? LENGTH : beat_fault;
    end

    always @* begin
        invalid_patch=0;
        for(integer i=0;i<4;i=i+1)
            if(edit_patch_keep[i] && {1'b0,edit_patch_offset}+17'(i)>=kept_count)
                invalid_patch=1;
    end

    // A final output beat can contain both edited data and CRC bytes.
    function automatic logic [31:0] update_crc(
        input logic [31:0] base,
        input logic [7:0] byte_value
    );
        logic [31:0] crc;
        crc=base ^ {24'd0,byte_value};
        for(integer bit_index=0;bit_index<8;bit_index=bit_index+1)
            crc=(crc>>1) ^ (crc[0] ? 32'hedb88320 : 0);
        update_crc=crc;
    endfunction

    always @* begin : emit
        integer at,source_at,patch_lane,trailer_at;
        logic [7:0] selected;
        m_data='0;
        m_keep='0;
        crc_after=crc_state;
        at=0;
        source_at=0;
        patch_lane=0;
        trailer_at=0;
        selected='0;
        for(integer lane=0;lane<OUT_BYTES;lane=lane+1) begin
            at=int'(send_at)+lane;
            if(m_valid && at<int'(total_length[reader])) begin
                m_keep[lane]=1;
                if(at<int'(body_length[reader])) begin
                    if(at<int'(prefix_length[reader])) begin
                        selected=prefix_bytes[reader][8*at+:8];
                    end else begin
                        source_at=at-int'(prefix_length[reader]);
                        patch_lane=source_at-int'(patch_from[reader]);
                        if(patch_lane>=0 && patch_lane<4 && patch_mask[reader][patch_lane])
                            selected=patch_value[reader][8*patch_lane+:8];
                        else
                            selected=mem[(reader ? MAX_FRAME : 0)+int'(keep_from[reader])+source_at];
                    end
                    if(with_crc[reader])
                        crc_after=update_crc(crc_after,selected);
                    m_data[8*lane+:8]=selected;
                end else begin
                    trailer_at=at-int'(body_length[reader]);
                    m_data[8*lane+:8]=8'((~crc_after)>>(8*trailer_at));
                end
            end
        end
    end

    always_ff @(posedge clk)
        if(capture_beat && beat_fault==OK)
            for(integer lane=0;lane<IN_BYTES;lane=lane+1)
                if(s_keep[lane])
                    mem[(writer ? MAX_FRAME : 0)+int'(measured)+lane-int'(first_lane)]
                        <=s_data[8*lane+:8];

    always_ff @(posedge clk) begin
        if(rst) begin
            {writer,reader,committed,with_crc,collecting,have_first,pending_report} <= '0;
            {declared,measured,sticky,send_at} <= '0;
            {report_tag,report_input_bytes,report_output_bytes,report_status} <= '0;
            crc_state <= CRC_SEED;
        end else begin
            if(report_ready)
                pending_report<=0;
            if(capture_descriptor) begin
                collecting<=1;
                have_first<=0;
                declared<=edit_length;
                measured<='0;
                sticky<=int'(edit_length)>MAX_FRAME ? TOO_LONG :
                        invalid_edit ? BAD_EDIT : invalid_patch ? BAD_PATCH : OK;
                bank_tag[writer]<=edit_tag;
                keep_from[writer]<=edit_trim_head;
                prefix_length[writer]<=edit_prefix_length;
                prefix_bytes[writer]<=edit_prefix;
                body_length[writer]<=body_count[15:0];
                total_length[writer]<=wire_count[15:0];
                patch_from[writer]<=edit_patch_offset;
                patch_mask[writer]<=edit_patch_keep;
                patch_value[writer]<=edit_patch_bytes;
                with_crc[writer]<=edit_crc;
            end
            if(capture_beat) begin
                have_first<=1;
                measured<=observed;
                sticky<=beat_fault;
                if(s_last) begin
                    collecting<=0;
                    pending_report<=1;
                    report_tag<=bank_tag[writer];
                    report_input_bytes<=observed;
                    report_output_bytes<=finish==OK ? total_length[writer] : 16'd0;
                    report_status<=finish;
                    if(finish==OK) begin
                        committed[writer]<=1;
                        writer<=!writer;
                    end
                end
            end
            if(retire_beat) begin
                crc_state<=crc_after;
                if(m_last) begin
                    committed[reader]<=0;
                    reader<=!reader;
                    send_at<='0;
                    crc_state<=CRC_SEED;
                end else begin
                    send_at<=send_at+16'(OUT_BYTES);
                end
            end
        end
    end
`ifdef FORMAL_SPLICE
    logic sampled=0;
    always_ff @(posedge clk) begin
        sampled<=1;
        if(!sampled) assume(rst);
        if(sampled && !rst && !$past(rst)) begin
            if($past(m_valid && !m_ready))
                assert(m_valid && $stable({m_data,m_keep,m_first,m_last,m_tag}));
            if($past(report_valid && !report_ready))
                assert(report_valid && $stable({report_tag,report_input_bytes,report_output_bytes,report_status}));
            if(m_valid) begin
                assert(total_length[reader]>send_at);
                assert(body_length[reader]<=total_length[reader]);
                assert(m_keep!=0 && (m_keep & (m_keep+1'b1))==0);
                assert(m_last || (&m_keep));
            end
            if(capture_beat && beat_fault==OK)
                assert(measured_after<=17'(MAX_FRAME) && !committed[writer]);
            if(collecting)
                assert(!pending_report);
            cover(m_valid && with_crc[reader] && send_at<body_length[reader] &&
                  {1'b0,send_at}+17'(OUT_BYTES)>{1'b0,body_length[reader]});
            cover(report_valid && report_status==BAD_PATCH);
            cover((&committed) && m_valid && !m_ready);
        end
    end
`endif

endmodule

`default_nettype wire
