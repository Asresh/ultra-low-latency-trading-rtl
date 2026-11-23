`timescale 1ns/1ps

// Simulation-only ID-zero AXI memory server. Bus errors may leave side
// effects. All accepted bursts are checked regardless of response value.
module tb_dma_memory #(
    parameter integer MEMORY_BYTES = 65536,
    parameter integer MAX_BURST = 16,
    parameter logic [31:0] SEED = 32'hff20d9c1
) (
    input wire clk,
    input wire reset,
    input wire arvalid,
    output wire arready,
    input wire [63:0] araddr,
    input wire [7:0] arlen,
    input wire [2:0] arsize,
    input wire [1:0] arburst,
    output logic rvalid,
    input wire rready,
    output logic [63:0] rdata,
    output logic [1:0] rresp,
    output logic rlast,
    input wire awvalid,
    output wire awready,
    input wire [63:0] awaddr,
    input wire [7:0] awlen,
    input wire [2:0] awsize,
    input wire [1:0] awburst,
    input wire wvalid,
    output wire wready,
    input wire [63:0] wdata,
    input wire [7:0] wstrb,
    input wire wlast,
    output logic bvalid,
    input wire bready,
    output logic [1:0] bresp,
    input wire block_read_address,
    input wire block_write_address,
    input wire block_write_response,
    input wire [63:0] inject_read_error,
    input wire [63:0] inject_write_error,
    input wire [63:0] inject_bad_last
);
    // The testbench initializes and inspects this byte array outside the
    // bus ports to model host-owned memory.
    logic [7:0] memory [0:MEMORY_BYTES-1];
    logic [31:0] random_bits;
    logic [63:0] rd_address, wr_address;
    logic reading, writing;
    logic write_response_wait;
    integer read_remaining, write_remaining;
    logic write_failed;
    integer ar_count, aw_count, r_count, w_count, b_count;
    integer read_stalls, write_stalls, response_stalls;
    integer page_ends_read, page_ends_write, short_writes;
    integer read_faults, write_faults;
    logic stall_ar, stall_aw, stall_w;
    logic [71:0] held_ar, held_aw;
    logic [72:0] held_w;

    assign arready = !reset && !reading && !rvalid && !block_read_address && random_bits[2:0] > 0;
    assign awready = !reset && !writing && !write_response_wait && !bvalid &&
                     !block_write_address && random_bits[6:4] > 1;
    assign wready = !reset && writing && random_bits[11:8] > 4;

    // Read an aligned 64-bit word from the byte-addressed memory model.
    function automatic logic [63:0] word(input logic [63:0] address);
        logic [63:0] result;
        begin
            for (integer lane = 0; lane < 8; lane++)
                result[8*lane+:8] = memory[int'(address)+lane];
            word = result;
        end
    endfunction

    // Write all eight little-endian bytes into the memory model.
    task automatic store_word(input logic [63:0] address, input logic [63:0] value);
        begin
            for (integer lane = 0; lane < 8; lane++)
                memory[int'(address)+lane] = value[8*lane+:8];
        end
    endtask

    // Initialize each memory byte to a known fill pattern.
    task automatic clear_memory(input logic [7:0] pattern);
        begin
            for (integer index = 0; index < MEMORY_BYTES; index++)
                memory[index] = pattern;
        end
    endtask

    // Validate the incoming AXI burst geometry before modeling memory accesses.
    task automatic check_burst(input logic [63:0] addr, input logic [7:0] length,
                               input logic [2:0] size, input logic [1:0] burst_kind);
        integer bytes;
        begin
            bytes = (int'(length)+1) * 8;
            if (size != 3'd3 || burst_kind != 2'b01 || addr[2:0] != 0 ||
                int'(length)+1 > MAX_BURST || addr + 64'(bytes) > MEMORY_BYTES)
                $fatal(1,"Invalid memory window/geometry at %h length %0d", addr, length);
            if (int'(addr[11:0]) + bytes > 4096)
                $fatal(1,"AXI burst crossed a 4-KiB boundary at %h", addr);
        end
    endtask

    always @(negedge clk) begin
        if (reset) random_bits <= SEED;
        else random_bits <= {random_bits[30:0], random_bits[31]^random_bits[21]^
                                               random_bits[1]^random_bits[0]};
    end

    always @(posedge clk) begin
        if (reset) begin
            reading <= 0;
            writing <= 0;
            rvalid <= 0;
            bvalid <= 0;
            rdata <= 0;
            rlast <= 0;
            rresp <= 0;
            bresp <= 0;
            rd_address <= 0;
            wr_address <= 0;
            read_remaining <= 0;
            write_remaining <= 0;
            write_response_wait <= 0;
            write_failed <= 0;
            ar_count <= 0;
            aw_count <= 0;
            r_count <= 0;
            w_count <= 0;
            b_count <= 0;
            read_stalls <= 0;
            write_stalls <= 0;
            response_stalls <= 0;
            page_ends_read <= 0;
            page_ends_write <= 0;
            short_writes <= 0;
            read_faults <= 0;
            write_faults <= 0;
            stall_ar <= 0;
            stall_aw <= 0;
            stall_w <= 0;
        end else begin
            if (stall_ar && (!arvalid || {araddr,arlen} !== held_ar))
                $fatal(1, "AR payload changed under backpressure");
            if (stall_aw && (!awvalid || {awaddr,awlen} !== held_aw))
                $fatal(1, "AW payload changed under backpressure");
            if (stall_w && (!wvalid || {wdata,wstrb,wlast} !== held_w))
                $fatal(1, "W payload changed under backpressure");
            stall_ar <= arvalid && !arready;
            stall_aw <= awvalid && !awready;
            stall_w <= wvalid && !wready;
            if (arvalid && !arready) begin held_ar <= {araddr,arlen}; read_stalls <= read_stalls+1; end
            if (awvalid && !awready) held_aw <= {awaddr,awlen};
            if (wvalid && !wready) begin held_w <= {wdata,wstrb,wlast}; write_stalls <= write_stalls+1; end
            if (write_response_wait && block_write_response) response_stalls <= response_stalls+1;

            if (arvalid && arready) begin
                check_burst(araddr, arlen, arsize, arburst);
                reading <= 1;
                rd_address <= araddr;
                read_remaining <= int'(arlen) + 1;
                ar_count <= ar_count+1;
                if (int'(araddr[11:0])+(int'(arlen)+1)*8 == 4096)
                    page_ends_read <= page_ends_read+1;
            end
            if (rvalid && rready) begin
                rvalid <= 0;
                r_count <= r_count+1;
                if (read_remaining == 1 || rd_address == inject_bad_last) begin
                    reading <= 0;
                    read_remaining <= 0;
                end else begin
                    rd_address <= rd_address+8;
                    read_remaining <= read_remaining-1;
                end
            end
            if (reading && !rvalid && random_bits[15:13] != 0) begin
                rvalid <= 1;
                rdata <= word(rd_address);
                rresp <= rd_address == inject_read_error ? 2'b10 : 2'b00;
                if (rd_address == inject_read_error) read_faults <= read_faults+1;
                rlast <= (read_remaining == 1) ^ (rd_address == inject_bad_last);
            end

            if (awvalid && awready) begin
                check_burst(awaddr, awlen, awsize, awburst);
                writing <= 1;
                wr_address <= awaddr;
                write_remaining <= int'(awlen)+1;
                write_failed <= 0;
                aw_count <= aw_count+1;
                if (int'(awaddr[11:0])+(int'(awlen)+1)*8 == 4096)
                    page_ends_write <= page_ends_write+1;
            end
            if (wvalid && wready) begin
                if (wlast != (write_remaining == 1) || wstrb == 0)
                    $fatal(1, "WLAST or strobe violates memory contract");
                if (wstrb != 8'hff)
                    short_writes <= short_writes + 1;
                for (integer byte_no = 0; byte_no < 8; byte_no++)
                    if (wstrb[byte_no])
                        memory[int'(wr_address)+byte_no] <= wdata[8*byte_no+:8];
                if (wr_address == inject_write_error) write_failed <= 1;
                w_count <= w_count+1;
                wr_address <= wr_address+8;
                write_remaining <= write_remaining-1;
                if (wlast) begin
                    writing <= 0;
                    write_response_wait <= 1;
                end
            end
            if (write_response_wait && !block_write_response && random_bits[20:17] != 0) begin
                write_response_wait <= 0;
                bvalid <= 1;
                bresp <= write_failed ? 2'b10 : 2'b00;
                if (write_failed) write_faults <= write_faults+1;
            end
            if (bvalid && bready) begin
                bvalid <= 0;
                b_count <= b_count+1;
            end
        end
    end
endmodule
