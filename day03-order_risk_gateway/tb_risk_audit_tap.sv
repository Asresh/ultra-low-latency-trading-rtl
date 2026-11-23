`timescale 1ns/1ps
`default_nettype none

module tb_risk_audit_tap #(
    parameter integer DEPTH = 3
);
    localparam integer RECORD_BITS = 16;
    localparam integer DROP_W = 3;
    localparam integer MAX_DROP = (1 << DROP_W) - 1;
    logic clk = 0;
    always #5 clk = ~clk;

    logic rst, s_valid, m_ready, a_ready;
    logic [RECORD_BITS-1:0] s_record;
    wire s_ready, m_valid, a_valid, drop_saturated;
    wire [RECORD_BITS-1:0] m_record, a_record;
    wire [DROP_W-1:0] dropped;

    risk_audit_tap #(
        .RECORD_BITS(RECORD_BITS), .AUDIT_DEPTH(DEPTH), .DROP_W(DROP_W), .LOSSLESS(1'b0)
    ) dut (.*);

    logic [RECORD_BITS-1:0] queue [0:2047];
    logic [RECORD_BITS-1:0] stalled_audit_record;
    integer head, tail, loss;
    integer total_primary = 0, total_audit = 0, total_drops = 0, exchanged_full = 0;
    logic audit_stalled;
    logic was_full;

    // Reference queue uses monotonic cursors, independent of the RTL pointer wrap.
    always @(posedge clk) begin
        if (rst) begin
            if (m_valid || a_valid || s_ready)
                $fatal(1, "tap handshake active during reset");
            head = 0;
            tail = 0;
            loss = 0;
            audit_stalled = 0;
        end else begin
            if (int'(dropped) != (loss > MAX_DROP ? MAX_DROP : loss) ||
                drop_saturated !== (loss > MAX_DROP))
                $fatal(1, "incorrect drop telemetry for %0d discarded records", loss);
            if (m_valid !== s_valid || s_ready !== m_ready || m_record !== s_record)
                $fatal(1, "best-effort primary path depended on audit capacity");
            if (a_valid !== (head != tail))
                $fatal(1, "audit queue valid disagrees with modeled occupancy");
            if (audit_stalled && (!a_valid || stalled_audit_record !== a_record))
                $fatal(1, "audit tap mutated a stalled head record");
            was_full = tail - head == DEPTH;
            if (a_valid && a_ready) begin
                if (a_record !== queue[head])
                    $fatal(1, "audit expected %h received %h", queue[head], a_record);
                head = head + 1;
                total_audit = total_audit + 1;
            end
            if (s_valid && s_ready) begin
                total_primary = total_primary + 1;
                if (tail - head == DEPTH) begin
                    loss = loss + 1;
                    total_drops = total_drops + 1;
                end else begin
                    if (was_full && a_ready) exchanged_full = exchanged_full + 1;
                    queue[tail] = s_record;
                    tail = tail + 1;
                end
            end
            audit_stalled = a_valid && !a_ready;
            stalled_audit_record = a_record;
        end
    end

    task automatic step(input integer record_id, input logic incoming, primary_ready, audit_ready);
        begin
            @(negedge clk);
            s_record = RECORD_BITS'(record_id);
            s_valid = incoming;
            m_ready = primary_ready;
            a_ready = audit_ready;
            @(posedge clk);
        end
    endtask

    logic [31:0] prng = 32'h0571_b32e;
    function automatic [31:0] random_next(input [31:0] value);
        logic [31:0] next;
        begin
            next = value ^ (value << 13);
            next = next ^ (next >> 17);
            random_next = next ^ (next << 5);
        end
    endfunction

    integer i, tag_number;
    logic pending;
    initial begin
        rst = 1;
        s_valid = 0;
        a_ready = 0;
        m_ready = 0;
        repeat (2) @(negedge clk);
        rst = 0;

        for (i = 0; i < DEPTH + MAX_DROP + 3; i = i + 1)
            step(i, 1, 1, 0);
        step(450, 1, 0, 0);
        step(450, 1, 0, 1);
        step(450, 1, 1, 1);
        for (i = 0; i < DEPTH + 2; i = i + 1)
            step(0, 0, 1, 1);
        @(negedge clk);
        if (a_valid || dropped != MAX_DROP[DROP_W-1:0] || !drop_saturated)
            $fatal(1, "burst did not saturate and drain");

        // Reset with unread observations; primary transfers after the new session
        // must not leak frames from the old one.
        for (i = 0; i < DEPTH; i = i + 1)
            step(500 + i, 1, 1, 0);
        @(negedge clk);
        rst = 1;
        s_valid = 0;
        repeat (2) @(negedge clk);
        rst = 0;
        step(0, 0, 1, 1);
        if (a_valid || dropped != 0 || drop_saturated)
            $fatal(1, "reset did not clear audit state");

        pending = 0;
        tag_number = 1000;
        for (i = 0; i < 900; i = i + 1) begin
            prng = random_next(prng);
            if (!pending) begin
                tag_number = tag_number + 1;
                pending = prng[1:0] != 0;
            end
            step(tag_number, pending, prng[6:4] < 6, prng[11:8] < 7);
            if (s_ready)
                pending = 0;
        end
        if (pending)
            step(tag_number, 1, 1, 1);
        for (i = 0; i < DEPTH + 2; i = i + 1)
            step(0, 0, 1, 1);
        @(negedge clk);
        if (a_valid || total_primary != total_audit + total_drops + DEPTH ||
            total_primary < 400 || exchanged_full < 20 || total_drops < 20)
            $fatal(1, "unexpected coverage: %0d primary %0d audit %0d drops %0d exchanges",
                   total_primary, total_audit, total_drops, exchanged_full);
        $display("PASS audit tap depth=%0d: %0d primary / %0d mirrored / %0d counted drops / %0d exchanges while full",
                 DEPTH, total_primary, total_audit, total_drops, exchanged_full);
        $finish;
    end

    initial begin
        #15000;
        $fatal(1, "audit tap watchdog");
    end
endmodule

`default_nettype wire
