`timescale 1ns/1ps
`default_nettype none

// Exact committed packets, retained across transport loss. The caller issues
// each write with its live cursor advance and starts a read only for an
// accepted replay login. The two operations are mutually exclusive.
module ouch_replay_window #(
    parameter integer DEPTH = 8,
    localparam integer ROW_W = DEPTH <= 1 ? 1 : $clog2(DEPTH),
    localparam integer COUNT_W = $clog2(DEPTH+1)
) (
    input wire clk,
    input wire rst,
    input wire [63:0] next_sequence,
    input ouch_wire_pkg::packet_t packet,
    input wire commit,
    input wire begin_replay,
    input wire [63:0] replay_start,
    input wire verify_next,
    input wire [63:0] replay_cursor,
    output wire [63:0] oldest_sequence,
    output wire packet_matches
);
    import ouch_wire_pkg::*;
    localparam integer ROW_MATH_W = COUNT_W + 1;

    packet_t records [0:DEPTH-1];
    packet_t expected;
    logic [ROW_W-1:0] write_row, read_row;
    logic [COUNT_W-1:0] occupied;
    logic [ROW_W-1:0] fetch_row;
    wire seek = begin_replay && replay_start < next_sequence;
    wire prefetch = verify_next && replay_cursor + 1'b1 < next_sequence;
    wire [63:0] back = next_sequence - replay_start;
    wire [ROW_MATH_W-1:0] before_wrap =
        ROW_MATH_W'(write_row) + ROW_MATH_W'(DEPTH) - ROW_MATH_W'(back);
    wire [ROW_MATH_W-1:0] after_wrap = before_wrap >= ROW_MATH_W'(DEPTH)
        ? before_wrap - ROW_MATH_W'(DEPTH) : before_wrap;

    assign oldest_sequence = next_sequence - 64'(occupied);
    assign packet_matches = packet == expected;

    function automatic logic [ROW_W-1:0] step(input logic [ROW_W-1:0] row);
        step = row == ROW_W'(DEPTH-1) ? '0 : row + 1'b1;
    endfunction

    // A new login reads the first covered position on the same edge as
    // socket activation. A successful replay comparison prefetches the next
    // word. A stalled notice never advances this read.
    assign fetch_row = seek ? ROW_W'(after_wrap) : step(read_row);

    always_ff @(posedge clk) begin
        if (rst) begin
            write_row <= '0;
            read_row <= '0;
            occupied <= '0;
        end else begin
            if (commit) begin
                write_row <= step(write_row);
                if (occupied < COUNT_W'(DEPTH))
                    occupied <= occupied + 1'b1;
            end
            if (seek || prefetch)
                read_row <= fetch_row;
        end
    end

    // Keep the data array and read register outside reset for RAM inference.
    always_ff @(posedge clk) begin
        if (!rst && commit)
            records[write_row] <= packet;
        if (!rst && (seek || prefetch))
            expected <= records[fetch_row];
    end

`ifdef FORMAL
    always_ff @(posedge clk) begin
        if (!rst) begin
            assert (occupied <= COUNT_W'(DEPTH));
            assert (int'(write_row) < DEPTH);
            if (seek || prefetch) assert (int'(fetch_row) < DEPTH);
            if (seek) assert (replay_start >= oldest_sequence);
            if (verify_next) assert (replay_cursor >= oldest_sequence && replay_cursor < next_sequence);
            assert (!(commit && (seek || prefetch)));
        end
    end
`endif
endmodule

`default_nettype wire
