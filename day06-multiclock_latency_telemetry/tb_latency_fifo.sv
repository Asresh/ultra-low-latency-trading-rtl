`timescale 1ns/1ps

module tb_latency_fifo #(
    parameter integer W_HALF = 3,
    parameter integer R_HALF = 5,
    parameter integer ADDRESS_W = 3
);
    localparam integer WIDTH = 33;
    localparam integer TARGET = 1700;
    logic wclk = 0;
    logic rclk = 0;
    always #(W_HALF) wclk = !wclk;
    always #(R_HALF) rclk = !rclk;

    logic power_reset = 0;
    wire wreset, rreset;
    logic s_valid = 0;
    wire s_ready;
    logic [WIDTH-1:0] s_data = '0;
    wire m_valid;
    logic m_ready = 0;
    wire [WIDTH-1:0] m_data;
    latency_reset_sync w_sync(.clk(wclk), .power_reset, .reset(wreset));
    latency_reset_sync r_sync(.clk(rclk), .power_reset, .reset(rreset));
    latency_async_fifo #(.WIDTH(WIDTH), .ADDRESS_W(ADDRESS_W)) dut (.*);

    integer writes = 0;
    integer reads = 0;
    integer write_stalls = 0;
    integer read_stalls = 0;
    logic writing = 0;
    logic reading = 0;
    logic randomize = 0;
    logic blocked = 0;
    logic [WIDTH-1:0] held = '0;
    logic [31:0] wrng = 32'h7c00192a, rrng = 32'h98fce03d;
    integer epoch = 0;

    function automatic logic [31:0] advance(input logic [31:0] x);
        logic [31:0] t;
        begin
            t = x ^ (x << 13);
            t = t ^ (t >> 17);
            advance = t ^ (t << 5);
        end
    endfunction

    function automatic logic [WIDTH-1:0] payload(input integer index, input integer generation);
        logic [31:0] scrambled;
        begin
            scrambled = advance(32'(index) ^ (32'd81919 * 32'(generation)));
            payload = {^scrambled, scrambled};
        end
    endfunction

    always @(negedge wclk) begin
        wrng = advance(wrng);
        if (!writing || wreset || writes >= TARGET) begin
            s_valid = 0;
        end else begin
            if (!s_valid || s_data != payload(writes, epoch))
                s_data = payload(writes, epoch);
            // Deterministic bursts, with an independent consumer pause schedule.
            if (randomize && !s_valid)
                s_valid = wrng[3:0] >= 3;
            else
                s_valid = 1;
        end
    end

    always @(negedge rclk) begin
        rrng = advance(rrng);
        m_ready = reading && !rreset && (!randomize || rrng[3:0] >= 5);
    end

    always @(posedge wclk) begin
        if (wreset)
            writes <= 0;
        else if (s_valid && s_ready)
            writes <= writes + 1;
        else if (s_valid && !s_ready)
            write_stalls <= write_stalls + 1;
    end

    always @(posedge rclk) begin
        if (rreset) begin
            reads <= 0;
            blocked <= 0;
            held <= '0;
        end else begin
            if (blocked && (!m_valid || held !== m_data))
                $fatal(1, "FIFO output changed through stall");
            blocked <= m_valid && !m_ready;
            if (m_valid && !m_ready) begin
                held <= m_data;
                read_stalls <= read_stalls + 1;
            end
            if (m_valid && m_ready) begin
                if (reads >= writes || m_data !== payload(reads, epoch))
                    $fatal(1, "FIFO epoch %0d item %0d saw %h expected %h", epoch, reads,
                           m_data, payload(reads, epoch));
                reads <= reads + 1;
            end
        end
    end

    task automatic shared_reset(input integer generation);
        begin
            writing = 0;
            reading = 0;
            power_reset = 1;
            repeat (6) @(negedge wclk);
            repeat (6) @(negedge rclk);
            epoch = generation;
            power_reset = 0;
            repeat (14) @(negedge wclk);
            repeat (14) @(negedge rclk);
            if (m_valid || !s_ready || writes != 0 || reads != 0)
                $fatal(1, "FIFO did not contain a clean generation");
        end
    endtask

    initial begin
        #1;
        shared_reset(1);
        writing = 1;
        reading = 0;
        repeat (60) @(negedge wclk);
        if (!m_valid || s_ready || writes < (1 << ADDRESS_W))
            $fatal(1, "Expected FIFO saturation before reset stress");
        #1;
        shared_reset(2);
        writing = 1;
        reading = 1;
        randomize = 1;
        wait (reads == TARGET);
        repeat (12) @(negedge rclk);
        if (m_valid || reads != writes || write_stalls == 0 || read_stalls == 0)
            $fatal(1, "FIFO missed a stall, failed to drain, or replayed stale items");
        $display("PASS CDC FIFO: %0d words, clocks %0d/%0d, depth %0d, %0d write/%0d read stalls",
                 reads, 2*W_HALF, 2*R_HALF, (1 << ADDRESS_W), write_stalls, read_stalls);
        $finish;
    end
    initial begin #180000; $fatal(1, "CDC FIFO timeout"); end
endmodule
