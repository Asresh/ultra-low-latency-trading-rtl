`timescale 1ns/1ps
`default_nettype none

module tb_ouch_framing;
    import ouch_wire_pkg::*;
    localparam integer CAPTURE_BYTES = 65536;
    localparam integer PACKETS = 1000;
    localparam integer WIRE_BITS = 8 * (MAX_BODY + 2);
    logic clk = 0;
    always #5 clk = ~clk;

    logic flush;
    logic in_valid;
    wire in_ready;
    logic [63:0] in_data;
    logic [7:0] in_keep;
    wire out_valid;
    logic out_ready;
    packet_t out_packet;
    status_t rx_fault;

    ouch_soup_rx rx (
        .clk, .flush, .s_valid(in_valid), .s_ready(in_ready),
        .s_data(in_data), .s_keep(in_keep), .m_valid(out_valid), .m_ready(out_ready),
        .m_packet(out_packet), .fault(rx_fault)
    );

    logic tx_input_valid;
    wire tx_input_ready;
    packet_t tx_input;
    wire tx_valid, tx_last;
    logic tx_ready;
    wire [63:0] tx_data;
    wire [7:0] tx_keep;

    ouch_soup_tx tx (
        .clk, .flush, .s_valid(tx_input_valid), .s_ready(tx_input_ready),
        .s_packet(tx_input), .m_valid(tx_valid), .m_ready(tx_ready),
        .m_data(tx_data), .m_keep(tx_keep), .m_last(tx_last)
    );

    logic [7:0] byte_stream [0:CAPTURE_BYTES-1];
    packet_t expected_packets [0:PACKETS-1];
    integer bytes_in_script = 0;
    integer script_packets = 0;
    logic check_tx = 0;
    integer rx_packets = 0, tx_packets = 0;
    integer rx_stall = 0, tx_stall = 0, keep_values = 0;
    integer tx_cursor = 0;
    integer rx_epoch = 0;
    logic [WIRE_BITS-1:0] captive_tx;
    logic [WIRE_BITS-1:0] expected_tx;
    packet_t tx_expected_packet;
    logic prev_rx_stall = 0, prev_tx_stall = 0;
    packet_t frozen_frame;
    logic [72:0] frozen_tx;

    function automatic [7:0] signature(
        input integer epoch, frame_index, byte_index
    );
        signature = 8'((epoch * 51 + (frame_index + 7) * 43 + byte_index * 139) ^
                       (frame_index >> 3) ^ (byte_index * byte_index));
    endfunction

    function automatic packet_t make_packet(
        input integer length, epoch, frame_index
    );
        packet_t frame;
        body_t payload;
        begin
            payload = '0;
            for (integer i = 0; i < MAX_BODY; i = i + 1) begin
                if (i < length)
                    payload[i*8 +: 8] = signature(epoch, frame_index, i);
            end
            frame = {frame_length_t'(length), payload};
            make_packet = frame;
        end
    endfunction

    function automatic [WIRE_BITS-1:0] to_wire(input packet_t frame);
        to_wire = {frame.body, 8'(frame.length), 8'h00};
    endfunction

    function automatic [31:0] scramble(input logic [31:0] raw);
        logic [31:0] x;
        begin
            x = raw ^ (raw << 13);
            x = x ^ (x >> 17);
            scramble = x ^ (x << 5);
        end
    endfunction

    logic random_ready = 1;
    logic allow_rx = 1;
    logic allow_tx = 1;
    logic [31:0] sink_seed = 32'h0231_7dac;
    always @(negedge clk) begin
        sink_seed <= scramble(sink_seed);
        out_ready <= allow_rx && (!random_ready || sink_seed[3:0] < 11);
        tx_ready <= allow_tx && (!random_ready || sink_seed[7:4] < 11);
    end

    always @(posedge clk) begin
        if (flush) begin
            if (in_ready || out_valid || tx_valid || tx_input_ready)
                $fatal(1, "byte boundary visible while flushing");
            prev_rx_stall = 0;
            prev_tx_stall = 0;
            tx_cursor = 0;
            captive_tx = '0;
        end else begin
            if (prev_rx_stall && (!out_valid || out_packet !== frozen_frame))
                $fatal(1, "independent RX contract violated under stall");
            if (prev_tx_stall && (!tx_valid || {tx_last, tx_keep, tx_data} !== frozen_tx))
                $fatal(1, "independent TX contract violated under stall");
            if (out_valid && out_ready) begin
                if (rx_packets >= script_packets || out_packet !== expected_packets[rx_packets])
                    $fatal(1, "RX epoch %0d packet %0d mismatch length=%0d",
                           rx_epoch, rx_packets, out_packet.length);
                rx_packets = rx_packets + 1;
            end
            if (check_tx && tx_valid && tx_ready) begin
                tx_expected_packet = expected_packets[tx_packets];
                expected_tx = to_wire(tx_expected_packet);
                if (!tx_keep || (tx_keep & (tx_keep + 1'b1)) != 0)
                    $fatal(1, "TX keep is not a nonzero low-lane mask");
                for (integer i = 0; i < 8; i = i + 1) if (tx_keep[i]) begin
                    if (tx_cursor >= MAX_BODY + 2)
                        $fatal(1, "TX emitted excess byte");
                    captive_tx[tx_cursor*8 +: 8] = tx_data[i*8 +: 8];
                    tx_cursor = tx_cursor + 1;
                end
                if (tx_last) begin
                    if (tx_cursor != int'(tx_expected_packet.length) + 2 ||
                        captive_tx !== expected_tx)
                        $fatal(1, "TX expected length/data mismatch at packet %0d", tx_packets);
                    tx_cursor = 0;
                    captive_tx = '0;
                    tx_packets = tx_packets + 1;
                end
            end
            if (in_valid && in_ready)
                keep_values = keep_values | (1 << $countones(in_keep));
            if (in_valid && !in_ready) rx_stall = rx_stall + 1;
            if (tx_valid && !tx_ready) tx_stall = tx_stall + 1;
            prev_rx_stall = out_valid && !out_ready;
            prev_tx_stall = tx_valid && !tx_ready;
            frozen_frame = out_packet;
            frozen_tx = {tx_last, tx_keep, tx_data};
        end
    end

    task automatic add_expected(input integer length);
        packet_t new_frame;
        logic [WIRE_BITS-1:0] physical;
        begin
            if (script_packets >= PACKETS || bytes_in_script + length + 2 >= CAPTURE_BYTES)
                $fatal(1, "test array capacity exceeded");
            new_frame = make_packet(length, rx_epoch, script_packets);
            expected_packets[script_packets] = new_frame;
            script_packets = script_packets + 1;
            physical = to_wire(new_frame);
            for (integer i = 0; i < length + 2; i = i + 1)
                byte_stream[bytes_in_script + i] = physical[i*8 +: 8];
            bytes_in_script = bytes_in_script + length + 2;
        end
    endtask

    task automatic ordered_poison_rx;
        integer first_byte;
        integer drained;
        begin
            drained = rx_packets;
            first_byte = bytes_in_script;
            add_expected(1);
            add_expected(2);
            allow_rx = 0;
            send_chunk(first_byte, 7);
            wait (out_valid);
            rx_beat(64'h1234, 8'h02);
            repeat (4) @(negedge clk);
            if (rx_fault != ST_OK || rx_packets != drained || !out_valid)
                $fatal(1, "later malformed beat passed a stalled complete frame");
            allow_rx = 1;
            wait_rx(drained + 2);
            repeat (3) @(negedge clk);
            if (rx_fault != ST_BAD_KEEP || in_ready || out_valid)
                $fatal(1, "malformed beat did not close RX after the ordered prefix");
            transport_reset();
        end
    endtask

    task automatic rx_beat(input logic [63:0] data, input logic [7:0] keep);
        begin
            @(negedge clk);
            in_valid = 1;
            in_data = data;
            in_keep = keep;
            do @(posedge clk); while (!in_ready);
            @(negedge clk);
            in_valid = 0;
        end
    endtask

    logic [31:0] driver_seed = 32'h39c8_0741;
    task automatic send_chunk(input integer index, length);
        logic [63:0] data;
        logic [7:0] keep;
        begin
            keep = 0;
            driver_seed = scramble(driver_seed);
            data = {driver_seed, ~driver_seed};
            for (integer lane = 0; lane < 8; lane = lane + 1) begin
                if (lane < length) begin
                    keep[lane] = 1'b1;
                    data[lane*8 +: 8] = byte_stream[index + lane];
                end
            end
            rx_beat(data, keep);
        end
    endtask

    task automatic transport_reset;
        begin
            @(negedge clk);
            flush = 1;
            in_valid = 0;
            tx_input_valid = 0;
            repeat (3) @(negedge clk);
            flush = 0;
            #1;
            if (rx_fault != ST_OK || out_valid || tx_valid)
                $fatal(1, "framing fault/data crossed reset");
        end
    endtask

    task automatic wait_rx(input integer stop_at);
        integer wait_cycles;
        begin
            wait_cycles = 0;
            while (rx_packets != stop_at) begin
                @(negedge clk);
                wait_cycles = wait_cycles + 1;
                if (wait_cycles > 200) $fatal(1, "RX lost a complete frame");
            end
        end
    endtask

    task automatic send_tx(input packet_t frame);
        begin
            @(negedge clk);
            tx_input = frame;
            tx_input_valid = 1;
            do @(posedge clk); while (!tx_input_ready);
            @(negedge clk);
            tx_input_valid = 0;
        end
    endtask

    task automatic sweep_rx;
        integer chunk;
        integer offset;
        begin
            // Vary lengths and alignment without adding padding between packets.
            for (integer stride = 1; stride <= 8; stride = stride + 1) begin
                offset = bytes_in_script;
                for (integer j = 1; j <= MAX_BODY; j = j + 1)
                    add_expected(j);
                while (offset < bytes_in_script) begin
                    driver_seed = scramble(driver_seed);
                    chunk = 1 + int'(driver_seed[11:0]) % stride;
                    if (chunk > bytes_in_script - offset) chunk = bytes_in_script - offset;
                    send_chunk(offset, chunk);
                    offset = offset + chunk;
                end
                wait_rx(script_packets);
            end
            if (rx_fault != ST_OK || keep_values != 'h1fe)
                $fatal(1, "framing sweep fault or insufficient geometry coverage");
        end
    endtask

    task automatic recovery_rx;
        integer split;
        integer started;
        integer old_packets;
        begin
            // Drop a socket at every interior byte boundary of a maximum frame.
            // The next connection begins at an independent frame boundary.
            for (split = 1; split < MAX_BODY + 2; split = split + 1) begin
                old_packets = rx_packets;
                started = bytes_in_script;
                add_expected(MAX_BODY);
                for (integer byte_num = 0; byte_num < split; byte_num = byte_num + 1)
                    send_chunk(started + byte_num, 1);
                repeat (2) @(negedge clk);
                if (rx_packets != old_packets)
                    $fatal(1, "truncated frame committed at byte %0d", split);
                script_packets = script_packets - 1;
                transport_reset();
                rx_epoch = rx_epoch + 1;
                started = bytes_in_script;
                add_expected(1 + split % MAX_BODY);
                for (integer i = started; i < bytes_in_script; i = i + 1)
                    send_chunk(i, 1);
                wait_rx(script_packets);
            end
        end
    endtask

    task automatic poison_rx;
        integer mask;
        logic valid_mask;
        begin
            // Check every noncontiguous mask, including zero and high-only.
            for (mask = 0; mask <= 255; mask = mask + 1) begin
                valid_mask = mask != 0 && ((mask & (mask + 1)) == 0);
                if (!valid_mask) begin
                    transport_reset();
                    rx_beat(64'h5555_aa00_ff12_0700, 8'(mask));
                    repeat (3) @(negedge clk);
                    if (rx_fault != ST_BAD_KEEP || in_ready)
                        $fatal(1, "mask 0x%02x did not close RX", mask);
                end
            end
            transport_reset();
            rx_beat(64'h0, 8'h03);
            repeat (3) @(negedge clk);
            if (rx_fault != ST_FRAME_LENGTH) $fatal(1, "zero length did not fault");
            transport_reset();
            rx_beat(64'hffff, 8'h03);
            repeat (3) @(negedge clk);
            if (rx_fault != ST_FRAME_LENGTH) $fatal(1, "oversize length did not fault");
            transport_reset();
        end
    endtask

    initial begin
        flush = 1;
        in_valid = 0;
        in_data = 0;
        in_keep = 0;
        tx_input_valid = 0;
        tx_input = '0;
        captive_tx = '0;
        repeat (3) @(negedge clk);
        flush = 0;
        sweep_rx();
        recovery_rx();
        poison_rx();
        ordered_poison_rx();

        check_tx = 1;
        for (integer i = 0; i < script_packets; i = i + 1)
            send_tx(expected_packets[i]);
        do @(negedge clk); while (tx_packets < script_packets);
        if (tx_valid || out_valid || rx_stall == 0 || tx_stall == 0)
            $fatal(1, "framing test did not drain or missed backpressure");

        $display("PASS Soup framing: %0d frames each direction, %0d partial socket cuts, every keep mask, stalls RX=%0d TX=%0d",
                 tx_packets, rx_epoch, rx_stall, tx_stall);
        $finish;
    end

    initial begin
        #5000000;
        $fatal(1, "Soup framing watchdog");
    end
endmodule

`default_nettype wire
