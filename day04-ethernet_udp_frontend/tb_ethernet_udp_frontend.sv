`timescale 1ns/1ps
`include "frontend_codes.svh"

module tb_ethernet_udp_frontend;
    localparam integer OP_WRITE = 0, OP_COMMIT = 1, OP_FLOWS = 2, OP_BURST = 3,
                       OP_WAIT = 4, OP_COUNTS = 5, OP_STAT = 6, OP_CLEAR = 7,
                       OP_PRECISION = 8;

    logic clk = 0;
    always #5 clk = !clk;
    logic rst = 1;

    logic rx_mac_valid = 0, rx_mac_ready;
    logic [63:0] rx_mac_data = 0;
    logic [7:0] rx_mac_keep = 0;
    logic rx_mac_first = 0, rx_mac_last = 0, rx_mac_bad = 0;
    logic rule_write_valid = 0, rule_write_ready;
    logic [2:0] rule_write_index = 0;
    logic [255:0] rule_write_data = 0;
    logic rule_commit_valid = 0, rule_commit_ready;
    logic [7:0] rule_epoch;
    logic rx_info_valid, rx_info_ready = 0;
    logic [383:0] rx_info_data;
    logic rx_payload_valid, rx_payload_ready = 0;
    logic [63:0] rx_payload_data;
    logic [7:0] rx_payload_keep;
    logic rx_payload_first, rx_payload_last;

    logic [1:0] tx_desc_valid = 0, tx_desc_ready;
    logic [639:0] tx_desc_data = 0;
    logic [1:0] tx_payload_valid = 0, tx_payload_ready;
    logic [127:0] tx_payload_data = 0;
    logic [15:0] tx_payload_keep = 0;
    logic [1:0] tx_payload_first = 0, tx_payload_last = 0;
    logic tx_mac_valid, tx_mac_ready = 0;
    logic [63:0] tx_mac_data;
    logic [7:0] tx_mac_keep;
    logic tx_mac_first, tx_mac_last, tx_mac_lane;
    logic tx_status_valid, tx_status_ready = 0;
    logic [127:0] tx_status_data;

    logic stats_snapshot = 0, stats_clear_live = 0;
    logic [4:0] stats_index = 0;
    logic [63:0] stats_data;
    logic [31:0] stats_snapshot_id;

    `ifdef DEFAULT_GEOMETRY
    localparam integer FRAME_LIMIT = 1536, PAYLOAD_LIMIT = 1472;
    `else
    localparam integer FRAME_LIMIT = 256, PAYLOAD_LIMIT = 192;
    `endif
    `ifdef STRICT_UDP
    localparam bit ACCEPT_ZERO = 0;
    `else
    localparam bit ACCEPT_ZERO = 1;
    `endif

    ethernet_udp_frontend #(
        .MAX_FRAME(FRAME_LIMIT), .MAX_PAYLOAD(PAYLOAD_LIMIT), .ALLOW_ZERO_UDP(ACCEPT_ZERO)
    ) dut (.*);

    integer clock_no = 0, command_no = 0, control_file, opcode, command_rc;
    integer rx_source_file, rx_info_file, rx_payload_file;
    integer tx_source_file [0:1], tx_mac_file [0:1], tx_status_file [0:1];
    integer frames_rx = 0, events_rx = 0, beats_rx = 0, beats_tx = 0, statuses_tx = 0;
    integer info_mode = 0, payload_mode = 0, mac_mode = 0, status_mode = 0;
    integer backpressure_mac_rx = 0, backpressure_rx_event = 0, backpressure_rx_data = 0;
    integer backpressure_tx_wire = 0, backpressure_tx_status = 0, duplex = 0;
    integer measured_rx_first = 0, measured_rx_info = 0, measured_tx_desc = 0;
    integer measured_tx_first = 0, measured_tx_gap = 0, prior_tx_transfer = 0;
    integer remaining_body = 0;
    logic receive_open = 0, receive_payload_open = 0, transmit_open = 0, active_tx_lane;
    logic [63:0] start_tick, ingress_tick [0:8191];
    logic [63:0] desc_tick [0:1], sent_tick [0:1];
    logic [319:0] a, b, c, d;
    logic [31:0] ready_rng = 32'h4e785f92;

    logic held_info = 0, held_body = 0, held_mac = 0, held_status = 0;
    logic [383:0] previous_info;
    logic [73:0] previous_body;
    logic [74:0] previous_mac;
    logic [127:0] previous_status;

    logic [383:0] info_expected, info_mask;
    logic [63:0] beat_expected;
    logic [7:0] keep_expected;
    logic first_expected, last_expected, ignored_bad;
    logic [63:0] status_hi_expected, status_stamp_expected;
    integer info_rc, body_rc, mac_rc, status_rc;
    integer observed_status_lane;
    string directory;

    function automatic logic [31:0] prng(input logic [31:0] state);
        logic [31:0] mixed;
        begin
            mixed = state ^ (state << 13);
            mixed = mixed ^ (mixed >> 17);
            prng = mixed ^ (mixed << 5);
        end
    endfunction

    function automatic logic sink_ready(input integer mode, input logic [2:0] random_bits);
        case (mode)
            1: sink_ready = 1;
            2: sink_ready = random_bits >= 3;
            default: sink_ready = 0;
        endcase
    endfunction

    function automatic integer width_of_keep(input logic [7:0] keep);
        integer count;
        begin
            count = 0;
            for (integer i=0; i<8; i=i+1)
                if (keep[i])
                    count = count + 1;
            width_of_keep = count;
        end
    endfunction

    always @(negedge clk) begin
        ready_rng = prng(ready_rng);
        rx_info_ready = sink_ready(info_mode, ready_rng[2:0]);
        rx_payload_ready = sink_ready(payload_mode, ready_rng[7:5]);
        tx_mac_ready = sink_ready(mac_mode, ready_rng[12:10]);
        tx_status_ready = sink_ready(status_mode, ready_rng[17:15]);
    end

    always @(posedge clk) begin
        clock_no = clock_no + 1;
        if (clock_no > 300000)
            $fatal(1, "watchdog expired at command %0d opcode %0d", command_no, opcode);
        if (!rst) begin
            if (held_info && (!rx_info_valid || rx_info_data !== previous_info))
                $fatal(1, "RX metadata moved under stall");
            if (held_body && (!rx_payload_valid ||
                {rx_payload_first,rx_payload_last,rx_payload_keep,rx_payload_data} !== previous_body))
                $fatal(1, "RX payload moved under stall");
            if (held_mac && (!tx_mac_valid ||
                {tx_mac_lane,tx_mac_first,tx_mac_last,tx_mac_keep,tx_mac_data} !== previous_mac))
                $fatal(1, "TX MAC output moved under stall");
            if (held_status && (!tx_status_valid || tx_status_data !== previous_status))
                $fatal(1, "TX completion moved under stall");

            held_info = rx_info_valid && !rx_info_ready;
            held_body = rx_payload_valid && !rx_payload_ready;
            held_mac = tx_mac_valid && !tx_mac_ready;
            held_status = tx_status_valid && !tx_status_ready;
            if (held_info) begin
                previous_info = rx_info_data;
                backpressure_rx_event = backpressure_rx_event + 1;
            end
            if (held_body) begin
                previous_body = {rx_payload_first,rx_payload_last,rx_payload_keep,rx_payload_data};
                backpressure_rx_data = backpressure_rx_data + 1;
            end
            if (held_mac) begin
                previous_mac = {tx_mac_lane,tx_mac_first,tx_mac_last,tx_mac_keep,tx_mac_data};
                backpressure_tx_wire = backpressure_tx_wire + 1;
            end
            if (held_status) begin
                previous_status = tx_status_data;
                backpressure_tx_status = backpressure_tx_status + 1;
            end
            if (rx_mac_valid && !rx_mac_ready)
                backpressure_mac_rx = backpressure_mac_rx + 1;
            if (rx_mac_valid && rx_mac_ready && tx_mac_valid && tx_mac_ready)
                duplex = duplex + 1;

            if (rx_mac_valid && rx_mac_ready) begin
                if (!receive_open) begin
                    start_tick = dut.tick;
                    if (frames_rx == 0)
                        measured_rx_first = clock_no;
                end
                receive_open = !rx_mac_last;
                if (rx_mac_last) begin
                    ingress_tick[frames_rx] = start_tick;
                    frames_rx = frames_rx + 1;
                end
            end
            if (rx_info_valid && rx_info_ready) begin
                if (remaining_body != 0 || events_rx >= frames_rx)
                    $fatal(1, "RX metadata is out of packet order");
                if (events_rx == 0)
                    measured_rx_info = clock_no;
                info_rc = $fscanf(rx_info_file, "%h %h", info_expected, info_mask);
                if (info_rc != 2 || (rx_info_data & info_mask) !== info_expected)
                    $fatal(1, "RX metadata %0d command %0d\nreceived %h\nexpected %h mask %h",
                           events_rx, command_no, rx_info_data, info_expected, info_mask);
                if (rx_info_data[351:288] !== ingress_tick[events_rx])
                    $fatal(1, "RX arrival tick does not match accepted first beat");
                events_rx = events_rx + 1;
                if (rx_info_data[383:376] == `FE_OK)
                    remaining_body = int'(rx_info_data[95:80]);
            end
            if (rx_payload_valid && rx_payload_ready) begin
                body_rc = $fscanf(rx_payload_file, "%h %h %d %d %d", beat_expected,
                                  keep_expected, first_expected, last_expected, ignored_bad);
                if (body_rc != 5 ||
                    {rx_payload_data,rx_payload_keep,rx_payload_first,rx_payload_last} !==
                    {beat_expected,keep_expected,first_expected,last_expected})
                    $fatal(1, "RX payload beat %0d command %0d got %h/%h, expected %h/%h",
                           beats_rx, command_no, rx_payload_data, rx_payload_keep,
                           beat_expected, keep_expected);
                if (remaining_body <= 0 || rx_payload_first == receive_payload_open)
                    $fatal(1, "RX payload lacks a corresponding metadata completion");
                receive_payload_open = !rx_payload_last;
                remaining_body = remaining_body - width_of_keep(rx_payload_keep);
                if (rx_payload_last != (remaining_body == 0))
                    $fatal(1, "RX payload ended at the wrong byte count");
                beats_rx = beats_rx + 1;
            end

            for (integer lane=0; lane<2; lane=lane+1)
                if (tx_desc_valid[lane] && tx_desc_ready[lane]) begin
                    desc_tick[lane] = dut.tick;
                    if (measured_tx_desc == 0)
                        measured_tx_desc = clock_no;
                end

            if (tx_mac_valid && tx_mac_ready) begin
                if (transmit_open) begin
                    if (tx_mac_first || tx_mac_lane != active_tx_lane)
                        $fatal(1, "TX interleaved packet streams");
                end else if (!tx_mac_first) begin
                    $fatal(1, "TX sent continuation without start");
                end
                if (!transmit_open) begin
                    sent_tick[tx_mac_lane] = dut.tick;
                    active_tx_lane = tx_mac_lane;
                    if (beats_tx == 0)
                        measured_tx_first = clock_no;
                end else if (statuses_tx == 0 && clock_no - prior_tx_transfer > measured_tx_gap) begin
                    measured_tx_gap = clock_no - prior_tx_transfer;
                end
                prior_tx_transfer = clock_no;
                transmit_open = !tx_mac_last;
                mac_rc = $fscanf(tx_mac_file[tx_mac_lane], "%h %h %d %d %d",
                                 beat_expected, keep_expected, first_expected, last_expected, ignored_bad);
                if (mac_rc != 5 || {tx_mac_data,tx_mac_keep,tx_mac_first,tx_mac_last} !==
                    {beat_expected,keep_expected,first_expected,last_expected})
                    $fatal(1, "TX lane%0d byte mismatch at command %0d beat %0d\n  got %h/%h want %h/%h",
                           tx_mac_lane, command_no, beats_tx, tx_mac_data, tx_mac_keep,
                           beat_expected, keep_expected);
                beats_tx = beats_tx + 1;
            end
            if (tx_status_valid && tx_status_ready) begin
                observed_status_lane = int'(tx_status_data[119:112]);
                if (observed_status_lane < 0 || observed_status_lane > 1)
                    $fatal(1, "invalid TX status lane");
                status_rc = $fscanf(tx_status_file[observed_status_lane], "%h", status_hi_expected);
                if (status_rc != 1 || tx_status_data[127:64] !== status_hi_expected)
                    $fatal(1, "TX status %0d command %0d got %h want %h",
                           statuses_tx, command_no, tx_status_data[127:64], status_hi_expected);
                status_stamp_expected = status_hi_expected[63:56] == `FE_OK
                                     ? sent_tick[observed_status_lane] : desc_tick[observed_status_lane];
                if (tx_status_data[63:0] !== status_stamp_expected)
                    $fatal(1, "TX completion timestamp invalid");
                statuses_tx = statuses_tx + 1;
            end
        end
    end

    task automatic maybe_pause(input integer mode, input logic [31:0] random_bits);
        begin
            if (mode != 0 && random_bits[2:0] < 3)
                @(negedge clk);
        end
    endtask

    task automatic feed_rx(input integer number, input integer mode);
        logic [63:0] lane_word;
        logic [7:0] keep;
        logic first, last, bad;
        logic [31:0] random_bits;
        integer status;
        begin
            random_bits = 32'h98f097be + 32'(command_no);
            for (integer index=0; index<number; index=index+1) begin
                status = $fscanf(rx_source_file, "%h %h %d %d %d", lane_word, keep, first, last, bad);
                if (status != 5)
                    $fatal(1, "could not read RX input beat %0d", index);
                @(negedge clk);
                random_bits = prng(random_bits);
                if (mode != 0 && random_bits[2:0] < 3) begin
                    rx_mac_valid = 0;
                    maybe_pause(mode, random_bits);
                end
                rx_mac_valid = 1;
                rx_mac_data = lane_word;
                rx_mac_keep = keep;
                rx_mac_first = first;
                rx_mac_last = last;
                rx_mac_bad = bad;
                do @(posedge clk); while (!rx_mac_ready);
            end
            if (number != 0) begin
                @(negedge clk);
                rx_mac_valid = 0;
            end
        end
    endtask

    task automatic feed_tx(input integer lane, input integer number, input integer mode);
        logic [319:0] descriptor_word;
        logic [63:0] lane_word;
        logic [7:0] keep;
        logic first, last, unused_bad;
        logic [31:0] random_bits;
        integer beats, status;
        begin
            random_bits = 32'h68b1762f + 32'(17*command_no+lane);
            for (integer i=0; i<number; i=i+1) begin
                status = $fscanf(tx_source_file[lane], "%h %d", descriptor_word, beats);
                if (status != 2)
                    $fatal(1, "could not read TX lane %0d descriptor %0d", lane, i);
                @(negedge clk);
                tx_desc_valid[lane] = 1;
                tx_desc_data[320*lane +: 320] = descriptor_word;
                do @(posedge clk); while (!tx_desc_ready[lane]);
                @(negedge clk);
                tx_desc_valid[lane] = 0;
                for (integer j=0; j<beats; j=j+1) begin
                    status = $fscanf(tx_source_file[lane], "%h %h %d %d %d",
                                      lane_word, keep, first, last, unused_bad);
                    if (status != 5)
                        $fatal(1, "could not read TX lane %0d payload %0d", lane, j);
                    random_bits = prng(random_bits);
                    maybe_pause(mode, random_bits);
                    tx_payload_valid[lane] = 1;
                    tx_payload_data[64*lane +: 64] = lane_word;
                    tx_payload_keep[8*lane +: 8] = keep;
                    tx_payload_first[lane] = first;
                    tx_payload_last[lane] = last;
                    do @(posedge clk); while (!tx_payload_ready[lane]);
                    @(negedge clk);
                    tx_payload_valid[lane] = 0;
                end
            end
        end
    endtask

    task automatic rule_write(input logic [2:0] index, input logic [255:0] rule);
        begin
            @(negedge clk);
            rule_write_valid = 1;
            rule_write_index = index;
            rule_write_data = rule;
            do @(posedge clk); while (!rule_write_ready);
            @(negedge clk);
            rule_write_valid = 0;
        end
    endtask

    task automatic rule_commit;
        logic [7:0] old_epoch;
        begin
            @(negedge clk);
            old_epoch = rule_epoch;
            rule_commit_valid = 1;
            do @(posedge clk); while (!rule_commit_ready);
            @(negedge clk);
            rule_commit_valid = 0;
            if (rule_epoch !== old_epoch + 8'd1)
                $fatal(1, "rule epoch failed to increment");
        end
    endtask

    task automatic expect_stat(input logic [4:0] index, input logic [63:0] wanted);
        logic [31:0] previous_id;
        begin
            @(negedge clk);
            previous_id = stats_snapshot_id;
            stats_snapshot = 1;
            stats_index = index;
            @(negedge clk);
            stats_snapshot = 0;
            if (stats_snapshot_id !== previous_id + 32'd1 || stats_data !== wanted)
                $fatal(1, "statistics index %0d got %0d expected %0d", index, stats_data, wanted);
        end
    endtask

    task automatic expect_timing(input integer rx_clocks, input integer tx_clocks,
                                 input integer tx_gap, input integer duplex_clocks);
        begin
            if (measured_rx_info-measured_rx_first != rx_clocks ||
                measured_tx_first-measured_tx_desc != tx_clocks ||
                measured_tx_gap != tx_gap || duplex != duplex_clocks)
                $fatal(1, "timing mismatch RX=%0d TX=%0d gap=%0d duplex=%0d",
                       measured_rx_info-measured_rx_first, measured_tx_first-measured_tx_desc,
                       measured_tx_gap, duplex);
            $display("Measured cycles: RX first-to-info=%0d, TX request-to-first=%0d, TX beat gap=%0d, full duplex=%0d",
                     rx_clocks, tx_clocks, tx_gap, duplex_clocks);
        end
    endtask

    initial begin
        if (!$value$plusargs("vectors=%s", directory))
            directory = "build";
        control_file = $fopen({directory,"/commands.txt"},"r");
        rx_source_file = $fopen({directory,"/rx_input.txt"},"r");
        rx_info_file = $fopen({directory,"/rx_info.txt"},"r");
        rx_payload_file = $fopen({directory,"/rx_data.txt"},"r");
        tx_source_file[0] = $fopen({directory,"/tx0_input.txt"},"r");
        tx_source_file[1] = $fopen({directory,"/tx1_input.txt"},"r");
        tx_mac_file[0] = $fopen({directory,"/tx0_wire.txt"},"r");
        tx_mac_file[1] = $fopen({directory,"/tx1_wire.txt"},"r");
        tx_status_file[0] = $fopen({directory,"/tx0_status.txt"},"r");
        tx_status_file[1] = $fopen({directory,"/tx1_status.txt"},"r");
        if (!control_file || !rx_source_file || !rx_info_file || !rx_payload_file ||
            !tx_source_file[0] || !tx_source_file[1] || !tx_mac_file[0] || !tx_mac_file[1] ||
            !tx_status_file[0] || !tx_status_file[1])
            $fatal(1, "failed opening fixture files at %s", directory);

        repeat (4) @(negedge clk);
        rst = 0;
        while (!$feof(control_file)) begin
            command_rc = $fscanf(control_file, "%d %h %h %h %h", opcode, a, b, c, d);
            if (command_rc == 5) begin
                command_no = command_no + 1;
                case (opcode)
                    OP_WRITE: rule_write(a[2:0], b[255:0]);
                    OP_COMMIT: rule_commit();
                    OP_FLOWS: begin
                        info_mode = int'(a);
                        payload_mode = int'(b);
                        mac_mode = int'(c);
                        status_mode = int'(d);
                    end
                    OP_BURST: fork
                        feed_rx(int'(a), int'(d));
                        feed_tx(0, int'(b), int'(d));
                        feed_tx(1, int'(c), int'(d));
                    join
                    OP_WAIT: repeat (int'(a)) @(negedge clk);
                    OP_COUNTS: begin
                        if (events_rx != int'(a) || beats_rx != int'(b) ||
                            beats_tx != int'(c) || statuses_tx != int'(d))
                            $fatal(1, "count mismatch cmd%0d got RX %0d/%0d TX %0d/%0d want RX %0d/%0d TX %0d/%0d",
                                   command_no, events_rx, beats_rx, beats_tx, statuses_tx,
                                   int'(a),int'(b),int'(c),int'(d));
                    end
                    OP_STAT: expect_stat(a[4:0], b[63:0]);
                    OP_CLEAR: begin
                        @(negedge clk);
                        stats_clear_live = 1;
                        @(negedge clk);
                        stats_clear_live = 0;
                    end
                    OP_PRECISION: expect_timing(int'(a),int'(b),int'(c),int'(d));
                    default: $fatal(1, "unexpected opcode %0d", opcode);
                endcase
            end else if (!$feof(control_file))
                $fatal(1, "bad command record after command %0d", command_no);
        end
        repeat (20) @(negedge clk);
        if ($fscanf(rx_info_file, "%h", info_expected) == 1 ||
            $fscanf(rx_payload_file, "%h", beat_expected) == 1 ||
            $fscanf(tx_status_file[0], "%h", status_hi_expected) == 1 ||
            $fscanf(tx_status_file[1], "%h", status_hi_expected) == 1 ||
            $fscanf(tx_mac_file[0], "%h", beat_expected) == 1 ||
            $fscanf(tx_mac_file[1], "%h", beat_expected) == 1)
            $fatal(1, "golden fixture not fully consumed");
        if (backpressure_mac_rx == 0 || backpressure_rx_event == 0 ||
            backpressure_rx_data == 0 || backpressure_tx_wire == 0 || backpressure_tx_status == 0)
            $fatal(1, "missing directed stall coverage");
        $display("PASS: %0d RX metadata, %0d RX data beats, %0d TX wire beats, %0d TX statuses",
                 events_rx, beats_rx, beats_tx, statuses_tx);
        $display("Stall cycles MAC RX=%0d RX info=%0d RX data=%0d MAC TX=%0d TX status=%0d",
                 backpressure_mac_rx, backpressure_rx_event, backpressure_rx_data,
                 backpressure_tx_wire, backpressure_tx_status);
        $finish;
    end
endmodule
