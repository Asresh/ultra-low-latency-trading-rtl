`timescale 1ns/1ps
`default_nettype none
`include "dma_protocol.svh"

module host_dma_queues #(
    parameter integer QUEUES = 2,
    parameter integer RING_LOG = 3,
    parameter integer MAX_PACKET_BYTES = 512,
    parameter integer MAX_BURST = 16,
    parameter integer CAPTURE_TIMEOUT = 128,
    parameter bit ENFORCE_QUEUE_ISOLATION = 1'b0,
    parameter integer MAX_SEGMENTS = 8,
    localparam integer Q_W = QUEUES > 1 ? $clog2(QUEUES) : 1,
    localparam integer WORDS = (MAX_PACKET_BYTES + 7) / 8,
    localparam integer WORD_W = (WORDS > 1) ? $clog2(WORDS) : 1,
    localparam integer WAIT_W = $clog2(CAPTURE_TIMEOUT + 1)
) (
    input wire clk,
    input wire reset,

    input wire cfg_valid,
    output wire cfg_ready,
    input wire [Q_W-1:0] cfg_queue,
    input wire cfg_enable,
    input wire [63:0] cfg_submission_base,
    input wire [63:0] cfg_completion_base,
    input wire [63:0] cfg_payload_low,
    input wire [63:0] cfg_payload_high,
    input wire [31:0] cfg_seed,
    input wire [7:0] cfg_irq_batch,
    input wire [31:0] cfg_irq_delay,
    output wire cfg_ack,
    output wire cfg_ok,

    input wire host_valid,
    output wire host_ready,
    input wire [Q_W-1:0] host_queue,
    input wire [31:0] host_submit_tail,
    input wire [31:0] host_complete_head,
    output wire host_ack,
    output wire host_ok,

    input wire [Q_W-1:0] inspect_queue,
    output wire inspect_enabled,
    output wire [31:0] inspect_sq_head,
    output wire [31:0] inspect_sq_tail,
    output wire [31:0] inspect_cq_head,
    output wire [31:0] inspect_cq_tail,
    output wire [7:0] inspect_irq_backlog,
    input wire [QUEUES-1:0] irq_ack,
    output wire [QUEUES-1:0] irq,

    output wire packet_valid,
    input wire packet_ready,
    output wire [63:0] packet_data,
    output wire [7:0] packet_keep,
    output wire packet_first,
    output wire packet_last,
    output wire [15:0] packet_tag,
    output wire [Q_W-1:0] packet_queue,
    output wire [63:0] packet_cookie,

    output wire capture_valid,
    input wire capture_ready,
    output wire [15:0] capture_tag,
    output wire [Q_W-1:0] capture_queue,
    output wire [31:0] capture_length,
    output wire capture_abort,
    input wire s_packet_valid,
    output wire s_packet_ready,
    input wire [63:0] s_packet_data,
    input wire [7:0] s_packet_keep,
    input wire s_packet_first,
    input wire s_packet_last,

    output wire arvalid,
    input wire arready,
    output wire [63:0] araddr,
    output wire [7:0] arlen,
    output wire [2:0] arsize,
    output wire [1:0] arburst,
    input wire rvalid,
    output wire rready,
    input wire [63:0] rdata,
    input wire [1:0] rresp,
    input wire rlast,
    output wire awvalid,
    input wire awready,
    output wire [63:0] awaddr,
    output wire [7:0] awlen,
    output wire [2:0] awsize,
    output wire [1:0] awburst,
    output wire wvalid,
    input wire wready,
    output wire [63:0] wdata,
    output wire [7:0] wstrb,
    output wire wlast,
    input wire bvalid,
    output wire bready,
    input wire [1:0] bresp,

    output wire busy,
    output logic [1:0] fatal_code,
    output logic [63:0] cycles,
    output logic [31:0] completed,
    output logic [31:0] rejected,
    output logic [63:0] pull_bytes,
    output logic [63:0] push_bytes
);
    localparam logic [4:0] IDLE = 0, DESC_CMD = 1, DESC_READ = 2, CHECK = 3,
                           PAYLOAD_READ_CMD = 4, PAYLOAD_READ = 5, PULL_SEND = 6,
                           PUSH_OFFER = 7, PUSH_COLLECT = 8, PUSH_DRAIN = 9,
                           PUSH_CMD = 10, PUSH_WRITE = 11, COMPLETE_CMD = 12,
                           COMPLETE_WRITE = 13, FAILED = 14, TABLE_CMD = 15,
                           TABLE_READ = 16, TABLE_VALIDATE = 17;
    localparam integer SEG_W = MAX_SEGMENTS > 1 ? $clog2(MAX_SEGMENTS) : 1;
    localparam integer TABLE_W = $clog2(2*MAX_SEGMENTS);

    logic [4:0] state;
    logic [Q_W-1:0] cursor, queue_q, pick_queue;
    logic found_queue;
    wire [QUEUES-1:0] eligible;
    wire [63:0] pick_submission, pick_completion, pick_low, pick_high;
    logic [63:0] sq_address, cq_address, region_low, region_high;
    logic [255:0] descriptor;
    logic [1:0] descriptor_word, completion_word;
    logic [WORD_W-1:0] data_word;
    logic [31:0] bytes_left;
    logic [WAIT_W-1:0] wait_count;
    logic [7:0] completion_status;
    logic [31:0] completion_count, completion_crc;
    logic [31:0] write_digest;
    logic [63:0] segment_address [0:MAX_SEGMENTS-1];
    logic [31:0] segment_length [0:MAX_SEGMENTS-1];
    logic segment_reserved [0:MAX_SEGMENTS-1];
    logic [SEG_W-1:0] segment;
    logic [TABLE_W-1:0] table_word;
    logic [31:0] validated_bytes;
    logic [63:0] completion_tick;
    logic abort_q;

    wire [7:0] operation = descriptor[63:56];
    wire [7:0] flags = descriptor[55:48];
    wire [15:0] tag = descriptor[47:32];
    wire [31:0] length = descriptor[31:0];
    wire [63:0] address = descriptor[127:64];
    wire [63:0] cookie = descriptor[191:128];
    wire [31:0] expected_crc = descriptor[223:192];
    wire [31:0] reserved_word = descriptor[255:224];
    wire is_segmented = operation == `DMA_GATHER || operation == `DMA_SCATTER ||
        operation == `DMA_SEGMENT_DIGEST;
    wire is_readback = operation == `DMA_READBACK || operation == `DMA_SEGMENT_DIGEST;
    wire is_pull = operation == `DMA_PULL || operation == `DMA_GATHER;
    wire is_push = operation == `DMA_PUSH || operation == `DMA_SCATTER;
    wire [32:0] rounded_length = ({1'b0,length} + 33'd7) & ~33'd7;
    wire [64:0] payload_end = {1'b0,address} + 65'(rounded_length);
    wire [64:0] table_end = {1'b0,address} + (65'(reserved_word) << 4);
    wire [63:0] transfer_address = is_segmented ? segment_address[segment] : address;
    wire [31:0] transfer_bytes = is_segmented ? segment_length[segment] : length;
    wire more_segments = is_segmented && 32'(segment) + 1 < reserved_word;

    wire transfer_shape_ok =
        length != 0 && length <= MAX_PACKET_BYTES && address[2:0] == 0 && address >= region_low &&
        (is_segmented ? table_end : payload_end) <= {1'b0,region_high};
    wire basic_format_ok =
        flags[7:3] == 0 && (flags[1] || expected_crc == 0) && (!flags[2] || is_push) &&
        (is_segmented ? reserved_word != 0 && reserved_word <= MAX_SEGMENTS : reserved_word == 0);

    function automatic logic [64:0] segment_end(
        input logic [63:0] base,
        input logic [31:0] count
    );
        segment_end = {1'b0, base} + ((65'(count)+65'd7) & ~65'd7);
    endfunction

    wire [64:0] selected_end = segment_end(segment_address[segment], segment_length[segment]);
    wire [32:0] next_validated = {1'b0,validated_bytes} + {1'b0,segment_length[segment]};
    logic intersects_previous;
    always @* begin
        intersects_previous = 1'b0;
        for (integer i = 0; i < MAX_SEGMENTS; i++)
            if (SEG_W'(i) < segment && {1'b0,segment_address[i]} < selected_end &&
                {1'b0,segment_address[segment]} < segment_end(segment_address[i],segment_length[i]))
                intersects_previous = 1'b1;
    end
    wire table_overlap = {1'b0,segment_address[segment]} < table_end && {1'b0,address} < selected_end;
    wire segment_ok = !segment_reserved[segment] && segment_length[segment] != 0 &&
        segment_address[segment][2:0] == 0 && segment_address[segment] >= region_low &&
        selected_end <= {1'b0,region_high} &&
        (!more_segments || segment_length[segment][2:0] == 0) &&
        !table_overlap && !intersects_previous && next_validated <= {1'b0,length} &&
        (more_segments || next_validated == {1'b0,length});

    logic [63:0] packet_memory [0:WORDS-1];
    wire rd_cmd_ready, rd_valid, rd_done, rd_fault, rd_protocol;
    wire [63:0] rd_data;
    wire [7:0] rd_keep;
    wire wr_cmd_ready, wr_ready, wr_done, wr_fault;
    wire from_read = (state == DESC_READ || state == PAYLOAD_READ) && rd_valid;
    wire [SEG_W-1:0] table_entry = SEG_W'(table_word >> 1);
    wire from_packet = s_packet_valid && s_packet_ready;
    wire [31:0] digest;
    wire checksum_ok = !flags[1] || digest == expected_crc;

    // Return a contiguous low-lane mask for up to one data beat.
    function automatic logic [7:0] keep_for(input logic [31:0] count);
        keep_for = count >= 8 ? 8'hff : (8'd1 << count[2:0]) - 1'b1;
    endfunction

    // Add the byte count to a 64-bit total, saturating on overflow.
    function automatic logic [63:0] add_bytes(
        input logic [63:0] a,
        input logic [31:0] b
    );
        logic [64:0] sum;
        begin
            sum = {1'b0,a} + 65'(b);
            add_bytes = sum[64] ? '1 : sum[63:0];
        end
    endfunction

    // Copy valid byte lanes and zero every lane outside the keep mask.
    function automatic logic [63:0] mask_invalid(
        input logic [63:0] source,
        input logic [7:0] keep
    );
        logic [63:0] result;
        begin
            result = '0;
            for (integer lane = 0; lane < 8; lane++)
                if (keep[lane]) result[8*lane+:8] = source[8*lane+:8];
            mask_invalid = result;
        end
    endfunction

    always_comb begin : choose_pending
        integer trial;
        found_queue = 1'b0;
        pick_queue = cursor;
        for (integer distance = 0; distance < QUEUES; distance++) begin
            trial = int'(cursor) + distance;
            if (trial >= QUEUES)
                trial = trial - QUEUES;
            if (!found_queue && eligible[trial]) begin
                found_queue = 1'b1;
                pick_queue = Q_W'(trial);
            end
        end
    end

    wire retire = !reset && state == COMPLETE_WRITE && wr_done && !wr_fault;
    dma_queue_table #(
        .QUEUES(QUEUES), .RING_LOG(RING_LOG), .ENFORCE_QUEUE_ISOLATION(ENFORCE_QUEUE_ISOLATION)
    ) control (
        .clk, .reset, .cfg_valid, .cfg_ready, .cfg_queue, .cfg_enable,
        .cfg_submission_base, .cfg_completion_base, .cfg_payload_low, .cfg_payload_high,
        .cfg_seed, .cfg_irq_batch, .cfg_irq_delay, .cfg_ack, .cfg_ok,
        .host_valid, .host_ready, .host_queue, .host_submit_tail, .host_complete_head,
        .host_ack, .host_ok, .active(state != IDLE), .active_queue(queue_q), .retire,
        .immediate_irq(flags[0]), .irq_ack, .irq, .eligible, .selected(pick_queue),
        .selected_sq_addr(pick_submission), .selected_cq_addr(pick_completion),
        .selected_payload_low(pick_low), .selected_payload_high(pick_high),
        .inspect_queue, .inspect_enabled, .inspect_sq_head, .inspect_sq_tail,
        .inspect_cq_head, .inspect_cq_tail, .inspect_irq_backlog
    );

    logic [63:0] completion_data;
    always @* begin
        case (completion_word)
            2'd0: completion_data = {completion_status,operation,tag,completion_count};
            2'd1: completion_data = cookie;
            2'd2: completion_data = {completion_crc,8'(queue_q),24'd0};
            default: completion_data = completion_tick;
        endcase
    end

    dma_axi64 #(.MAX_BURST(MAX_BURST)) memory_transport (
        .clk, .reset,
        .rd_cmd_valid(state == DESC_CMD || state == TABLE_CMD || state == PAYLOAD_READ_CMD), .rd_cmd_ready,
        .rd_cmd_addr(state == DESC_CMD ? sq_address : (state == TABLE_CMD ? address : transfer_address)),
        .rd_cmd_bytes(state == DESC_CMD ? 32'd32 :
            (state == TABLE_CMD ? reserved_word << 4 : transfer_bytes)),
        .rd_valid, .rd_ready(state == DESC_READ || state == TABLE_READ || state == PAYLOAD_READ),
        .rd_data, .rd_keep, .rd_last(), .rd_done, .rd_fault, .rd_protocol,
        .wr_cmd_valid((state == PUSH_CMD && checksum_ok) || state == COMPLETE_CMD), .wr_cmd_ready,
        .wr_cmd_addr(state == COMPLETE_CMD ? cq_address : transfer_address),
        .wr_cmd_bytes(state == COMPLETE_CMD ? 32'd32 : transfer_bytes),
        .wr_valid(state == PUSH_WRITE || state == COMPLETE_WRITE), .wr_ready,
        .wr_data(state == COMPLETE_WRITE ? completion_data : packet_memory[data_word]),
        .wr_done, .wr_fault,
        .arvalid, .arready, .araddr, .arlen, .arsize, .arburst,
        .rvalid, .rready, .rdata, .rresp, .rlast,
        .awvalid, .awready, .awaddr, .awlen, .awsize, .awburst,
        .wvalid, .wready, .wdata, .wstrb, .wlast, .bvalid, .bready, .bresp
    );

    wire input_last_expected = bytes_left <= 8;
    wire capture_shape = s_packet_first == (bytes_left == length) &&
                         s_packet_last == input_last_expected &&
                         s_packet_keep == keep_for(bytes_left);
    wire capture_timeout = wait_count == WAIT_W'(CAPTURE_TIMEOUT-1);
    dma_crc32 checksum (
        .clk, .reset, .seed(state == CHECK || (state == PAYLOAD_READ_CMD && is_push && segment == 0)),
        .take((state == PAYLOAD_READ && from_read) ||
            (state == PUSH_COLLECT && from_packet && capture_shape)),
        .data(state == PAYLOAD_READ ? rd_data : s_packet_data),
        .keep(state == PAYLOAD_READ ? rd_keep : s_packet_keep),
        .digest
    );

    assign packet_valid = !reset && state == PULL_SEND;
    assign packet_data = mask_invalid(packet_memory[data_word], packet_keep);
    assign packet_keep = keep_for(bytes_left);
    assign packet_last = packet_valid && bytes_left <= 8;
    assign packet_first = packet_valid && bytes_left == length;
    assign packet_tag = tag;
    assign packet_queue = queue_q;
    assign packet_cookie = cookie;
    assign capture_valid = !reset && state == PUSH_OFFER;
    assign capture_tag = tag;
    assign capture_queue = queue_q;
    assign capture_length = length;
    assign capture_abort = abort_q;
    assign s_packet_ready = !reset && (state == PUSH_COLLECT || state == PUSH_DRAIN);
    assign busy = state != IDLE;

    always_ff @(posedge clk) begin
        if (!reset && state == PAYLOAD_READ && is_pull && from_read)
            packet_memory[data_word] <= rd_data;
        if (!reset && state == PUSH_COLLECT && from_packet && capture_shape)
            packet_memory[data_word] <= mask_invalid(s_packet_data, s_packet_keep);
        if (!reset && state == TABLE_READ && rd_valid) begin
            if (table_word[0]) begin
                segment_length[table_entry] <= rd_data[31:0];
                segment_reserved[table_entry] <= |rd_data[63:32];
            end else begin
                segment_address[table_entry] <= rd_data;
            end
        end
    end

    // Stage completion status, byte count and CRC before starting the CQ write.
    task automatic finish(input logic [7:0] status, input logic [31:0] count, input logic [31:0] crc);
        begin
            completion_status <= status;
            completion_count <= count;
            completion_crc <= crc;
            completion_tick <= cycles;
            completion_word <= 0;
            state <= COMPLETE_CMD;
        end
    endtask
    always_ff @(posedge clk) begin
        if (reset) begin
            state <= IDLE;
            descriptor <= '0;
            descriptor_word <= '0;
            completion_word <= '0;
            completion_status <= '0;
            completion_count <= '0;
            completion_crc <= '0;
            write_digest <= '0;
            segment <= '0;
            table_word <= '0;
            validated_bytes <= '0;
            completion_tick <= '0;
            data_word <= '0;
            bytes_left <= '0;
            cursor <= '0;
            queue_q <= '0;
            region_low <= '0;
            region_high <= '0;
            sq_address <= '0;
            cq_address <= '0;
            wait_count <= '0;
            abort_q <= 0;
            fatal_code <= 0;
            cycles <= 0;
            completed <= 0;
            rejected <= 0;
            pull_bytes <= 0;
            push_bytes <= 0;
        end else begin
            cycles <= cycles + 1'b1;
            abort_q <= 1'b0;
            case (state)
                IDLE: if (!cfg_valid && found_queue) begin
                    queue_q <= pick_queue;
                    sq_address <= pick_submission;
                    cq_address <= pick_completion;
                    region_low <= pick_low;
                    region_high <= pick_high;
                    cursor <= pick_queue == Q_W'(QUEUES-1) ? 0 : pick_queue + 1'b1;
                    descriptor <= '0;
                    descriptor_word <= '0;
                    state <= DESC_CMD;
                end
                DESC_CMD: if (rd_cmd_ready)
                    state <= DESC_READ;
                DESC_READ: begin
                    if (from_read) begin
                        descriptor[64*descriptor_word+:64] <= rd_data;
                        descriptor_word <= descriptor_word + 1'b1;
                    end
                    if (rd_done) begin
                        if (rd_protocol) begin
                            fatal_code <= 1;
                            state <= FAILED;
                        end else if (rd_fault) begin
                            descriptor <= '0;
                            finish(`DMA_READ,0,0);
                        end else begin
                            state <= CHECK;
                        end
                    end
                end
                CHECK: begin
                    data_word <= '0;
                    bytes_left <= length;
                    segment <= '0;
                    table_word <= '0;
                    validated_bytes <= '0;
                    if (!basic_format_ok) begin
                        finish(`DMA_BAD_DESC,0,0);
                    end else if (operation == `DMA_FENCE) begin
                        if (length != 0 || address != 0 || flags[1])
                            finish(`DMA_BAD_DESC,0,0);
                        else
                            finish(`DMA_OK,0,0);
                    end else if (!is_pull && !is_push && !is_readback) begin
                        finish(`DMA_BAD_OP,0,0);
                    end else if (!transfer_shape_ok) begin
                        finish(`DMA_ADDRESS,0,0);
                    end else if (is_segmented) begin
                        state <= TABLE_CMD;
                    end else if (is_push) begin
                        state <= PUSH_OFFER;
                    end else begin
                        state <= PAYLOAD_READ_CMD;
                    end
                end
                TABLE_CMD: if (rd_cmd_ready)
                    state <= TABLE_READ;
                TABLE_READ: begin
                    if (rd_valid)
                        table_word <= table_word + 1'b1;
                    if (rd_done) begin
                        if (rd_protocol) begin
                            fatal_code <= 1;
                            state <= FAILED;
                        end else if (rd_fault) begin
                            finish(`DMA_READ,0,0);
                        end else begin
                            state <= TABLE_VALIDATE;
                        end
                    end
                end
                TABLE_VALIDATE: begin
                    if (!segment_ok) begin
                        finish(`DMA_SEGMENT,0,0);
                    end else if (more_segments) begin
                        validated_bytes <= next_validated[31:0];
                        segment <= segment + 1'b1;
                    end else begin
                        segment <= '0;
                        state <= is_push ? PUSH_OFFER : PAYLOAD_READ_CMD;
                    end
                end
                PAYLOAD_READ_CMD: if (rd_cmd_ready)
                    state <= PAYLOAD_READ;
                PAYLOAD_READ: begin
                    if (from_read && is_pull)
                        data_word <= data_word + 1'b1;
                    if (rd_done) begin
                        if (rd_protocol) begin
                            fatal_code <= 1;
                            state <= FAILED;
                        end else if (rd_fault) begin
                            finish(`DMA_READ,0,0);
                        end else if (more_segments) begin
                            segment <= segment + 1'b1;
                            state <= PAYLOAD_READ_CMD;
                        end else if (is_push) begin
                            if (write_digest == digest)
                                finish(`DMA_OK,length,digest);
                            else
                                finish(`DMA_VERIFY,0,digest);
                        end else if (!checksum_ok) begin
                            finish(`DMA_CRC,0,digest);
                        end else if (is_pull) begin
                            data_word <= 0;
                            state <= PULL_SEND;
                        end else begin
                            finish(`DMA_OK,length,digest);
                        end
                    end
                end
                PULL_SEND: if (packet_valid && packet_ready) begin
                    if (packet_last)
                        finish(`DMA_OK,length,digest);
                    else begin
                        bytes_left <= bytes_left - 8;
                        data_word <= data_word + 1'b1;
                    end
                end
                PUSH_OFFER: if (capture_valid && capture_ready) begin
                    wait_count <= 0;
                    state <= PUSH_COLLECT;
                end
                PUSH_COLLECT: begin
                    if (from_packet) begin
                        wait_count <= 0;
                        if (!capture_shape) begin
                            if (s_packet_last)
                                finish(`DMA_FRAME,0,0);
                            else
                                state <= PUSH_DRAIN;
                        end else if (input_last_expected) begin
                            data_word <= 0;
                            state <= PUSH_CMD;
                        end else begin
                            bytes_left <= bytes_left - 8;
                            data_word <= data_word + 1'b1;
                        end
                    end else if (capture_timeout) begin
                        abort_q <= 1;
                        finish(`DMA_TIMEOUT,0,0);
                    end else begin
                        wait_count <= wait_count + 1'b1;
                    end
                end
                PUSH_DRAIN: begin
                    if (from_packet && s_packet_last) begin
                        finish(`DMA_FRAME,0,0);
                    end else if (capture_timeout) begin
                        abort_q <= 1;
                        finish(`DMA_TIMEOUT,0,0);
                    end else begin
                        wait_count <= wait_count + 1'b1;
                    end
                end
                PUSH_CMD: begin
                    if (!checksum_ok)
                        finish(`DMA_CRC,0,digest);
                    else if (wr_cmd_ready)
                        state <= PUSH_WRITE;
                end
                PUSH_WRITE: begin
                    if (wr_ready)
                        data_word <= data_word + 1'b1;
                    if (wr_done) begin
                        if (wr_fault)
                            finish(`DMA_WRITE,0,digest);
                        else if (more_segments) begin
                            segment <= segment + 1'b1;
                            state <= PUSH_CMD;
                        end
                        else if (flags[2]) begin
                            write_digest <= digest;
                            segment <= '0;
                            state <= PAYLOAD_READ_CMD;
                        end else begin
                            finish(`DMA_OK,length,digest);
                        end
                    end
                end
                COMPLETE_CMD: if (wr_cmd_ready)
                    state <= COMPLETE_WRITE;
                COMPLETE_WRITE: begin
                    if (wr_ready)
                        completion_word <= completion_word + 1'b1;
                    if (wr_done) begin
                        if (wr_fault) begin
                            fatal_code <= 2;
                            state <= FAILED;
                        end else begin
                            if (completed != '1)
                                completed <= completed + 1'b1;
                            if (completion_status == `DMA_OK) begin
                                if (is_pull) pull_bytes <= add_bytes(pull_bytes,length);
                                if (is_push) push_bytes <= add_bytes(push_bytes,length);
                            end else if (rejected != '1) begin
                                rejected <= rejected + 1'b1;
                            end
                            state <= IDLE;
                        end
                    end
                end
                FAILED: state <= FAILED;
                default: state <= FAILED;
            endcase
        end
    end
endmodule

`default_nettype wire
