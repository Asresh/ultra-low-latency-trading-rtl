`timescale 1ns/1ps
`default_nettype none

// ID-zero AXI4 INCR transport with one outstanding burst per direction.
module dma_axi64 #(
    parameter integer MAX_BURST = 16,
    localparam integer BURST_W = $clog2(MAX_BURST + 1)
) (
    input  wire          clk,
    input  wire          reset,
    input  wire          rd_cmd_valid,
    output wire          rd_cmd_ready,
    input  wire [63:0]   rd_cmd_addr,
    input  wire [31:0]   rd_cmd_bytes,
    output wire          rd_valid,
    input  wire          rd_ready,
    output wire [63:0]   rd_data,
    output wire [7:0]    rd_keep,
    output wire          rd_last,
    output logic         rd_done,
    output logic         rd_fault,
    output logic         rd_protocol,
    input  wire          wr_cmd_valid,
    output wire          wr_cmd_ready,
    input  wire [63:0]   wr_cmd_addr,
    input  wire [31:0]   wr_cmd_bytes,
    input  wire          wr_valid,
    output wire          wr_ready,
    input  wire [63:0]   wr_data,
    output logic         wr_done,
    output logic         wr_fault,
    output wire          arvalid,
    input  wire          arready,
    output wire [63:0]   araddr,
    output wire [7:0]    arlen,
    output wire [2:0]    arsize,
    output wire [1:0]    arburst,
    input  wire          rvalid,
    output wire          rready,
    input  wire [63:0]   rdata,
    input  wire [1:0]    rresp,
    input  wire          rlast,
    output wire          awvalid,
    input  wire          awready,
    output wire [63:0]   awaddr,
    output wire [7:0]    awlen,
    output wire [2:0]    awsize,
    output wire [1:0]    awburst,
    output wire          wvalid,
    input  wire          wready,
    output wire [63:0]   wdata,
    output wire [7:0]    wstrb,
    output wire          wlast,
    input  wire          bvalid,
    output wire          bready,
    input  wire [1:0]    bresp
);
    localparam logic [1:0] IDLE = 0, COMMAND = 1, TRANSFER = 2, RESPOND = 3;
    logic [1:0] rd_state, wr_state;
    logic [63:0] read_address, write_address;
    logic [31:0] read_left, write_left;
    logic [BURST_W-1:0] read_beats, write_beats;

    function automatic logic [BURST_W-1:0] burst_beats(
        input logic [11:0] start, input logic [31:0] remaining
    );
        logic [31:0] until_end;
        logic [12:0] until_boundary;
        logic [31:0] planned;
        begin
            until_end = (remaining >> 3) + 32'(|remaining[2:0]);
            until_boundary = 13'd4096 - {1'b0, start};
            planned = until_end > MAX_BURST ? MAX_BURST : until_end;
            if (planned > 32'(until_boundary >> 3))
                planned = 32'(until_boundary >> 3);
            burst_beats = BURST_W'(planned);
        end
    endfunction

    function automatic logic [7:0] byte_enable(input logic [31:0] bytes);
        byte_enable = bytes >= 8 ? 8'hff : (8'd1 << bytes[2:0]) - 1'b1;
    endfunction

    wire [BURST_W-1:0] planned_read = burst_beats(read_address[11:0], read_left);
    wire [BURST_W-1:0] planned_write = burst_beats(write_address[11:0], write_left);
    wire read_beat = rvalid && rready;
    wire write_beat = wvalid && wready;

    assign arvalid = !reset && rd_state == COMMAND;
    assign araddr = read_address;
    assign arlen = 8'(planned_read) - 1'b1;
    assign arsize = 3'd3;
    assign arburst = 2'b01;
    assign awvalid = !reset && wr_state == COMMAND;
    assign awaddr = write_address;
    assign awlen = 8'(planned_write) - 1'b1;
    assign awsize = 3'd3;
    assign awburst = 2'b01;

    assign rd_cmd_ready = !reset && rd_state == IDLE;
    assign rd_valid = !reset && rd_state == TRANSFER && rvalid;
    assign rready = !reset && rd_state == TRANSFER && rd_ready;
    assign rd_data = rdata;
    assign rd_keep = byte_enable(read_left);
    assign rd_last = rd_valid && read_left <= 8;

    assign wr_cmd_ready = !reset && wr_state == IDLE;
    assign wr_ready = !reset && wr_state == TRANSFER && wready;
    assign wvalid = !reset && wr_state == TRANSFER && wr_valid;
    assign wdata = wr_data;
    assign wstrb = byte_enable(write_left);
    assign wlast = wvalid && write_beats == 1;
    assign bready = !reset && wr_state == RESPOND;

    always_ff @(posedge clk) begin
        if (reset) begin
            rd_state <= IDLE;
            read_address <= '0;
            read_left <= '0;
            read_beats <= '0;
            rd_done <= 1'b0;
            rd_fault <= 1'b0;
            rd_protocol <= 1'b0;
        end else begin
            rd_done <= 1'b0;
            case (rd_state)
                IDLE: if (rd_cmd_valid && rd_cmd_ready) begin
                    read_address <= rd_cmd_addr;
                    read_left <= rd_cmd_bytes;
                    rd_fault <= 1'b0;
                    rd_protocol <= 1'b0;
                    if (rd_cmd_bytes == 0 || rd_cmd_addr[2:0] != 0) begin
                        rd_fault <= 1'b1;
                        rd_done <= 1'b1;
                    end else begin
                        rd_state <= COMMAND;
                    end
                end
                COMMAND: if (arvalid && arready) begin
                    rd_state <= TRANSFER;
                    read_beats <= planned_read;
                end
                TRANSFER: if (read_beat) begin
                    if (rresp != 0)
                        rd_fault <= 1'b1;
                    if (rlast != (read_beats == 1)) begin
                        rd_protocol <= 1'b1;
                        rd_done <= 1'b1;
                        rd_state <= IDLE;
                    end else begin
                        read_address <= read_address + 8;
                        read_left <= read_left <= 8 ? 32'd0 : read_left - 8;
                        read_beats <= read_beats - 1'b1;
                        if (read_left <= 8) begin
                            rd_done <= 1'b1;
                            rd_state <= IDLE;
                        end else if (read_beats == 1) begin
                            rd_state <= COMMAND;
                        end
                    end
                end
                default: rd_state <= IDLE;
            endcase
        end
    end

    always_ff @(posedge clk) begin
        if (reset) begin
            wr_state <= IDLE;
            write_address <= '0;
            write_left <= '0;
            write_beats <= '0;
            wr_done <= 1'b0;
            wr_fault <= 1'b0;
        end else begin
            wr_done <= 1'b0;
            case (wr_state)
                IDLE: if (wr_cmd_valid && wr_cmd_ready) begin
                    write_address <= wr_cmd_addr;
                    write_left <= wr_cmd_bytes;
                    wr_fault <= 1'b0;
                    if (wr_cmd_bytes == 0 || wr_cmd_addr[2:0] != 0) begin
                        wr_fault <= 1'b1;
                        wr_done <= 1'b1;
                    end else begin
                        wr_state <= COMMAND;
                    end
                end
                COMMAND: if (awvalid && awready) begin
                    wr_state <= TRANSFER;
                    write_beats <= planned_write;
                end
                TRANSFER: if (write_beat) begin
                    write_address <= write_address + 8;
                    write_left <= write_left <= 8 ? 32'd0 : write_left - 8;
                    write_beats <= write_beats - 1'b1;
                    if (write_beats == 1)
                        wr_state <= RESPOND;
                end
                RESPOND: if (bvalid && bready) begin
                    if (bresp != 0) begin
                        wr_fault <= 1'b1;
                        wr_done <= 1'b1;
                        wr_state <= IDLE;
                    end else if (write_left == 0) begin
                        wr_done <= 1'b1;
                        wr_state <= IDLE;
                    end else begin
                        wr_state <= COMMAND;
                    end
                end
                default: wr_state <= IDLE;
            endcase
        end
    end
endmodule

`default_nettype wire
