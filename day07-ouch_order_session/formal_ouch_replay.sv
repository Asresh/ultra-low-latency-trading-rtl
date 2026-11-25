`timescale 1ns/1ps
`default_nettype none

// Use a newest-first shift register as an independent model of the ring.
// Two arbitrary payload fields exercise distinct areas of the binary record.
module formal_ouch_replay #(
    parameter integer DEPTH = 3
) (
    input wire clk,
    input wire rst,
    input wire commit,
    input wire begin_replay,
    input wire verify_next,
    input wire [15:0] sample,
    input wire [7:0] replay_start
);
    import ouch_wire_pkg::*;

    localparam integer COUNT_W = $clog2(DEPTH+1);
    logic [7:0] next_sequence, replay_cursor;
    logic [15:0] chronological [0:DEPTH-1];
    logic [COUNT_W-1:0] filled;
    logic active, initialized = 1'b0;
    packet_t packet;
    wire [63:0] oldest_sequence;
    wire packet_matches;
    wire [7:0] age = next_sequence - replay_cursor;
    wire [7:0] model_first = next_sequence - 8'(filled);

    assign packet.length = 7'd72;
    assign packet.body = {sample[15:8],552'd0,sample[7:0],8'h53};

    ouch_replay_window #(.DEPTH(DEPTH)) dut (
        .clk, .rst, .next_sequence(64'(next_sequence)), .packet, .commit, .begin_replay,
        .replay_start(64'(replay_start)), .verify_next, .replay_cursor(64'(replay_cursor)),
        .oldest_sequence, .packet_matches
    );

    always @(posedge clk) begin
        initialized <= 1'b1;
        if (!initialized)
            assume (rst);

        if (rst) begin
            active <= 1'b0;
            filled <= 0;
            next_sequence <= 1;
            replay_cursor <= 1;
        end else begin
            assume (!(commit && (active || begin_replay || verify_next)));
            assume (!(begin_replay && verify_next));
            if (begin_replay) begin
                assume (replay_start >= model_first && replay_start <= next_sequence);
                replay_cursor <= replay_start;
                active <= replay_start < next_sequence;
            end
            if (verify_next) begin
                assume (active);
                assume (sample == chronological[int'(age)-1]);
                replay_cursor <= replay_cursor + 1'b1;
                active <= replay_cursor + 1'b1 < next_sequence;
            end
            if (commit) begin
                chronological[0] <= sample;
                for (integer index = 1; index < DEPTH; index = index + 1)
                    chronological[index] <= chronological[index-1];
                if (filled < COUNT_W'(DEPTH))
                    filled <= filled + 1;
                next_sequence <= next_sequence + 1'b1;
            end

            assert (oldest_sequence == 64'(model_first));
            if (active) begin
                assert (age > 0 && age <= 8'(filled));
                assert (packet_matches == (sample == chronological[int'(age)-1]));
            end
        end
    end
endmodule

`default_nettype wire
