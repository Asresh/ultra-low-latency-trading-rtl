`timescale 1ns/1ps
`default_nettype none

// Preserve one primary decision stream and fork transferred records to a bounded
// telemetry queue. Best effort mode protects primary issue latency under audit stalls.
module risk_audit_tap #(
    parameter integer RECORD_BITS = 192,
    parameter integer AUDIT_DEPTH = 4,
    parameter integer DROP_W = 32,
    parameter bit LOSSLESS = 1'b1,
    localparam integer PTR_W = AUDIT_DEPTH > 1 ? $clog2(AUDIT_DEPTH) : 1,
    localparam integer USED_W = $clog2(AUDIT_DEPTH + 1)
) (
    input  wire                   clk,
    input  wire                   rst,
    input  wire                   s_valid,
    output wire                   s_ready,
    input  wire [RECORD_BITS-1:0] s_record,
    output wire                   m_valid,
    input  wire                   m_ready,
    output wire [RECORD_BITS-1:0] m_record,
    output wire                   a_valid,
    input  wire                   a_ready,
    output wire [RECORD_BITS-1:0] a_record,
    output reg  [DROP_W-1:0]      dropped,
    output reg                    drop_saturated
);
    localparam [PTR_W-1:0] LAST = PTR_W'(AUDIT_DEPTH - 1);
    localparam [USED_W-1:0] CAPACITY = USED_W'(AUDIT_DEPTH);

    reg [RECORD_BITS-1:0] buffer [0:AUDIT_DEPTH-1];
    reg [PTR_W-1:0] write_ptr, read_ptr;
    reg [USED_W-1:0] used;
    wire full = used == CAPACITY;
    wire route_primary = !LOSSLESS || !full;
    wire transfer = s_valid && s_ready;
    wire audit_pop = a_valid && a_ready;
    wire audit_push = transfer && (!full || audit_pop);

    assign s_ready = !rst && m_ready && route_primary;
    assign m_valid = !rst && s_valid && route_primary;
    assign m_record = s_record;
    assign a_valid = !rst && used != '0;
    assign a_record = buffer[read_ptr];

    function automatic [PTR_W-1:0] next_ptr(input [PTR_W-1:0] old);
        next_ptr = old == LAST ? '0 : old + 1'b1;
    endfunction

    always @(posedge clk) begin
        if (rst) begin
            write_ptr <= '0;
            read_ptr <= '0;
            used <= '0;
            dropped <= '0;
            drop_saturated <= 1'b0;
        end else begin
            if (audit_push) begin
                buffer[write_ptr] <= s_record;
                write_ptr <= next_ptr(write_ptr);
            end
            if (audit_pop)
                read_ptr <= next_ptr(read_ptr);
            case ({audit_push, audit_pop})
                2'b10: used <= used + 1'b1;
                2'b01: used <= used - 1'b1;
                default: used <= used;
            endcase

            if (transfer && !audit_push) begin
                if (&dropped)
                    drop_saturated <= 1'b1;
                else
                    dropped <= dropped + 1'b1;
            end
        end
    end
endmodule

`default_nettype wire
