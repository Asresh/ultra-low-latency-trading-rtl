`timescale 1ns/1ps
`default_nettype none

// Independently latch AW, W, and AR; serialize transactions at the register
// bank while allowing the read and write responses to stall independently.
module latency_axil #(
    parameter integer ADDRESS_W = 12
) (
    input  wire                 clk,
    input  wire                 reset,
    input  wire [ADDRESS_W-1:0] s_awaddr,
    input  wire                 s_awvalid,
    output wire                 s_awready,
    input  wire [31:0]          s_wdata,
    input  wire [3:0]           s_wstrb,
    input  wire                 s_wvalid,
    output wire                 s_wready,
    output logic [1:0]          s_bresp,
    output logic                s_bvalid,
    input  wire                 s_bready,
    input  wire [ADDRESS_W-1:0] s_araddr,
    input  wire                 s_arvalid,
    output wire                 s_arready,
    output logic [31:0]         s_rdata,
    output logic [1:0]          s_rresp,
    output logic                s_rvalid,
    input  wire                 s_rready,
    output wire                 req_valid,
    input  wire                 req_ready,
    output wire                 req_write,
    output wire [ADDRESS_W-1:0] req_addr,
    output wire [31:0]          req_data,
    output wire [3:0]           req_strobe,
    input  wire                 rsp_valid,
    output wire                 rsp_ready,
    input  wire [31:0]          rsp_data,
    input  wire [1:0]           rsp_status
);
    logic have_aw, have_w, have_ar;
    logic [ADDRESS_W-1:0] awbuf, arbuf;
    logic [31:0] wbuf;
    logic [3:0] strobes;
    logic outstanding, outstanding_write;
    logic prefer_write;
    logic hold_request, held_write;

    wire write_space = !s_bvalid || s_bready;
    wire read_space = !s_rvalid || s_rready;
    wire can_write = have_aw && have_w && write_space;
    wire can_read = have_ar && read_space;
    wire selected_write = hold_request ? held_write : can_write && (prefer_write || !can_read);
    wire issued = req_valid && req_ready;

    assign s_awready = !reset && !have_aw;
    assign s_wready = !reset && !have_w;
    assign s_arready = !reset && !have_ar;
    assign req_valid = !reset && !outstanding && (hold_request || can_write || can_read);
    assign req_write = selected_write;
    assign req_addr = selected_write ? awbuf : arbuf;
    assign req_data = selected_write ? wbuf : '0;
    assign req_strobe = selected_write ? strobes : '0;
    assign rsp_ready = !reset && outstanding && (outstanding_write ? write_space : read_space);

    always_ff @(posedge clk) begin
        if (reset) begin
            have_aw <= 1'b0;
            have_w <= 1'b0;
            have_ar <= 1'b0;
            awbuf <= '0;
            arbuf <= '0;
            wbuf <= '0;
            strobes <= '0;
            s_bresp <= '0;
            s_bvalid <= 1'b0;
            s_rdata <= '0;
            s_rresp <= '0;
            s_rvalid <= 1'b0;
            outstanding <= 1'b0;
            outstanding_write <= 1'b0;
            prefer_write <= 1'b1;
            hold_request <= 1'b0;
            held_write <= 1'b0;
        end else begin
            if (s_awvalid && s_awready) begin
                have_aw <= 1'b1;
                awbuf <= s_awaddr;
            end
            if (s_wvalid && s_wready) begin
                have_w <= 1'b1;
                wbuf <= s_wdata;
                strobes <= s_wstrb;
            end
            if (s_arvalid && s_arready) begin
                have_ar <= 1'b1;
                arbuf <= s_araddr;
            end
            if (s_bvalid && s_bready)
                s_bvalid <= 1'b0;
            if (s_rvalid && s_rready)
                s_rvalid <= 1'b0;
            if (req_valid && !req_ready) begin
                hold_request <= 1'b1;
                held_write <= selected_write;
            end
            if (issued) begin
                outstanding <= 1'b1;
                outstanding_write <= selected_write;
                hold_request <= 1'b0;
                prefer_write <= !selected_write;
                if (selected_write) begin
                    have_aw <= 1'b0;
                    have_w <= 1'b0;
                end else begin
                    have_ar <= 1'b0;
                end
            end
            if (rsp_valid && rsp_ready) begin
                outstanding <= 1'b0;
                if (outstanding_write) begin
                    s_bvalid <= 1'b1;
                    s_bresp <= rsp_status;
                end else begin
                    s_rvalid <= 1'b1;
                    s_rresp <= rsp_status;
                    s_rdata <= rsp_data;
                end
            end
        end
    end
endmodule

`default_nettype wire
