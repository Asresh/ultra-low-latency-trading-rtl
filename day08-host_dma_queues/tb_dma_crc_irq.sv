`timescale 1ns/1ps
`default_nettype none

module tb_dma_crc_irq;
    logic clk = 0;
    always #5 clk = ~clk;

    logic reset, seed, take;
    logic [63:0] data;
    logic [7:0] keep;
    wire [31:0] digest;
    dma_crc32 crc (.clk, .reset, .seed, .take, .data, .keep, .digest);

    logic clear, completion, force_notification, acknowledge;
    logic [7:0] minimum_batch;
    logic [31:0] maximum_delay;
    wire request;
    wire [7:0] unacknowledged;
    dma_interrupts irq (
        .clk, .reset, .clear, .completion, .force_notification, .acknowledge,
        .minimum_batch, .maximum_delay, .request, .unacknowledged
    );

    function automatic [31:0] reverse32(input logic [31:0] value);
        for (integer i = 0; i < 32; i = i + 1)
            reverse32[i] = value[31-i];
    endfunction

    // Independent reference keeps a forward polynomial register. It consumes
    // the right-to-left bits within each byte, then reflects only the output.
    function automatic [31:0] forward_byte(
        input logic [31:0] start,
        input logic [7:0] byte_data
    );
        logic [31:0] next;
        logic overflow;
        begin
            next = start;
            for (integer bit_no = 0; bit_no < 8; bit_no = bit_no + 1) begin
                overflow = next[31] != byte_data[bit_no];
                next = next << 1;
                if (overflow)
                    next = next ^ 32'h04c1_1db7;
            end
            forward_byte = next;
        end
    endfunction

    logic [31:0] crc_state;
    integer clock_id = 0;
    int unsigned oldest, pending;
    bit expected_request;
    integer crc_beats = 0, crc_bytes = 0, seeds = 0, crc_ignores = 0;
    integer irq_edges = 0, forced = 0, irq_acks = 0, irq_saturated = 0;
    integer ack_and_completion = 0, batch_notifications = 0, timer_notifications = 0;
    logic [255:0] mask_coverage = '0;
    logic [31:0] expected_digest;

    always @(posedge clk) begin
        clock_id++;
        if (reset || seed) begin
            crc_state = '1;
            if (seed) seeds++;
        end else if (take) begin
            crc_beats++;
            mask_coverage[keep] = 1'b1;
            for (integer byte_i = 0; byte_i < 8; byte_i = byte_i + 1) begin
                if (keep[byte_i]) begin
                    crc_state = forward_byte(crc_state, data[byte_i*8 +: 8]);
                    crc_bytes++;
                end
            end
        end else begin
            crc_ignores++;
        end

        begin : track_irq
            logic previously_asserted;
            logic next_batch, next_timer;
            previously_asserted = expected_request;
            if (reset || clear) begin
                pending = 0;
                oldest = 0;
                expected_request = 0;
            end else begin
                if (acknowledge) begin
                    pending = 0;
                    expected_request = 0;
                    irq_acks++;
                    if (completion) ack_and_completion++;
                end
                if (completion) begin
                    if (pending == 0) oldest = clock_id;
                    if (pending == 255) irq_saturated++;
                    else pending++;
                    if (force_notification) forced++;
                end
                if (pending == 0) begin
                    oldest = 0;
                end
                next_batch = pending != 0 && minimum_batch != 0 && pending >= int'(minimum_batch);
                next_timer = pending != 0 && maximum_delay != 0 && clock_id - oldest + 1 >= maximum_delay;
                if (next_batch && !expected_request) batch_notifications++;
                if (next_timer && !expected_request) timer_notifications++;
                expected_request = expected_request ||
                                   (completion && force_notification) || next_batch || next_timer;
            end
            if (expected_request && !previously_asserted) irq_edges++;
        end

        expected_digest = ~reverse32(crc_state);
        #1;
        if (digest !== expected_digest)
            $fatal(1, "CRC data %h keep %b expected %h observed %h",
                   data, keep, expected_digest, digest);
        if ({request, unacknowledged} !== {expected_request, 8'(pending)})
            $fatal(1, "IRQ expected %b/%0d observed %b/%0d at cycle %0d",
                   expected_request, pending, request, unacknowledged, clock_id);
    end

    task automatic set_crc(
        input logic start_seed,
        input logic enabled,
        input logic [63:0] new_data,
        input logic [7:0] mask
    );
        begin
            seed = start_seed;
            take = enabled;
            data = new_data;
            keep = mask;
        end
    endtask

    task automatic set_irq(
        input logic should_clear, completed, notify, host_ack,
        input integer batch,
        input logic [31:0] delay_cycles
    );
        begin
            clear = should_clear;
            completion = completed;
            force_notification = notify;
            acknowledge = host_ack;
            minimum_batch = 8'(batch);
            maximum_delay = delay_cycles;
        end
    endtask

    task automatic crc_once(
        input logic start_seed,
        input logic enabled,
        input logic [63:0] new_data,
        input logic [7:0] mask
    );
        begin
            @(negedge clk);
            set_crc(start_seed, enabled, new_data, mask);
            @(posedge clk);
            #2;
        end
    endtask

    task automatic irq_once(
        input logic should_clear, completed, notify, host_ack,
        input integer batch,
        input logic [31:0] delay_cycles
    );
        begin
            @(negedge clk);
            set_irq(should_clear, completed, notify, host_ack, batch, delay_cycles);
            @(posedge clk);
            #2;
        end
    endtask

    task automatic check_crc_golden;
        begin
            crc_once(1, 1, 64'hface_9481_6bb8_fed6, 8'hff);
            if (digest != 0)
                $fatal(1, "seed should take priority over a data beat");
            crc_once(0, 1, 64'h3837_3635_3433_3231, 8'hff);
            crc_once(0, 0, 64'haaaa_aaaa_aaaa_aaaa, 8'hff);
            crc_once(0, 1, 64'hfedc_ba98_7654_3239, 8'h01);
            if (digest != 32'hcbf4_3926)
                $fatal(1, "CRC-32 check string 123456789 mismatch");

            crc_once(1, 0, '0, '0);
            crc_once(0, 1, 64'hffff_ffff_ffff_ffff, 8'h00);
            if (digest != 0)
                $fatal(1, "empty keep changed CRC of empty input");
            crc_once(0, 1, 64'h7a59_9f85_7eb2_1022, 8'hb5);
            crc_once(0, 1, 64'h83c1_1139_4980_1132, 8'h42);
            crc_once(0, 1, 64'h9ed1_139a_bad6_8705, 8'h81);
            for (integer mask = 0; mask < 256; mask = mask + 1) begin
                crc_once(1, 0, 0, 0);
                crc_once(0, 1, 64'hffe2_c39a_4201_7e81, 8'(mask));
                crc_once(0, 1, 64'h0123_4567_89ab_cdef, 8'(255-mask));
            end
        end
    endtask

    task automatic check_irq_moderation;
        begin
            @(negedge clk);
            set_crc(0, 0, 0, 0);
            irq_once(1, 1, 1, 1, 1, 1);
            if (request || unacknowledged)
                $fatal(1, "clear must override notification, acknowledge, and completion");

            irq_once(0, 1, 0, 0, 3, 0);
            irq_once(0, 0, 1, 0, 3, 0);
            if (request || unacknowledged != 1)
                $fatal(1, "force without completion must not request");
            irq_once(0, 1, 0, 0, 3, 0);
            irq_once(0, 1, 0, 0, 3, 0);
            if (!request || unacknowledged != 3)
                $fatal(1, "third completion failed IRQ batch");
            irq_once(0, 0, 0, 0, 100, 0);
            if (!request)
                $fatal(1, "raised IRQ must remain until acknowledgment");
            irq_once(0, 1, 0, 1, 1, 0);
            if (!request || unacknowledged != 1)
                $fatal(1, "same edge acknowledge and new completion must be retained");
            irq_once(0, 0, 0, 1, 255, 0);

            irq_once(0, 1, 0, 0, 0, 4);
            if (request)
                $fatal(1, "moderation timer fired on first event");
            irq_once(0, 0, 0, 0, 0, 4);
            irq_once(0, 0, 0, 0, 0, 4);
            if (request)
                $fatal(1, "moderation timer fired one cycle early");
            irq_once(0, 0, 0, 0, 0, 4);
            if (!request)
                $fatal(1, "moderation timer missed exact boundary");
            irq_once(0, 1, 1, 1, 0, 0);
            if (!request || unacknowledged != 1)
                $fatal(1, "force completion lost during IRQ acknowledge");
            irq_once(0, 0, 0, 1, 0, 0);

            for (integer i = 0; i < 278; i = i + 1)
                irq_once(0, 1, 0, 0, 0, '1);
            if (request || unacknowledged != 255)
                $fatal(1, "IRQ backlog wrapped or a disabled batch fired");
            irq_once(0, 0, 0, 0, 255, 0);
            if (!request)
                $fatal(1, "reprogrammed batch comparator ignored saturated count");
            irq_once(0, 1, 0, 1, 0, 0);
            if (request || unacknowledged != 1)
                $fatal(1, "acknowledgment did not restart saturated state cleanly");
            irq_once(1, 1, 1, 0, 1, 1);
        end
    endtask

    function automatic [63:0] scramble64(input logic [63:0] value);
        logic [63:0] x;
        begin
            x = value ^ (value << 13);
            x = x ^ (x >> 7);
            scramble64 = x ^ (x << 17);
        end
    endfunction

    logic [63:0] rng = 64'hdb39_631a_fe20_4b88;
    logic [63:0] payload;
    logic [31:0] delay_value;
    initial begin
        reset = 1;
        set_crc(0, 0, 0, 0);
        set_irq(0, 0, 0, 0, 0, 0);
        repeat (3) @(negedge clk);
        reset = 0;
        check_crc_golden();
        check_irq_moderation();
        for (integer i = 0; i < 5000; i = i + 1) begin
            @(negedge clk);
            rng = scramble64(rng);
            payload = scramble64(rng ^ 64'h9e37_79b9_7f4a_7c15);
            set_crc(rng[15:10] == 1, rng[18:16] != 0, payload, rng[27:20]);
            delay_value = rng[30:28] == 0 ? '1 : 32'(rng[35:31] >> 1);
            set_irq(rng[41:36] == 0, rng[44:42] < 5, rng[47:45] == 0, rng[51:48] < 2,
                    int'(rng[57:52] >> 2), delay_value);
            @(posedge clk);
            #2;
        end
        if (&mask_coverage !== 1'b1 || crc_beats < 4000 || crc_bytes < 15000 ||
            crc_ignores < 300 || irq_edges < 200 || irq_saturated < 20 ||
            ack_and_completion < 100 || batch_notifications < 40 || timer_notifications < 50)
            $fatal(1, "coverage below target masks=%0d CRC=%0d bytes=%0d ignored=%0d IRQ=%0d sat=%0d simultaneous=%0d batch=%0d timer=%0d",
                   $countones(mask_coverage), crc_beats, crc_bytes, crc_ignores,
                   irq_edges, irq_saturated, ack_and_completion, batch_notifications, timer_notifications);
        $display("PASS CRC-32/IRQ: %0d CRC beats, %0d selected bytes, every mask, %0d seeds, %0d ignored cycles",
                 crc_beats, crc_bytes, seeds, crc_ignores);
        $display("IRQ edges=%0d forces=%0d acks=%0d saturated=%0d simultaneous=%0d batch=%0d timer=%0d",
                 irq_edges, forced, irq_acks, irq_saturated, ack_and_completion,
                 batch_notifications, timer_notifications);
        $finish;
    end

    initial begin
        #2000000;
        $fatal(1, "CRC/IRQ standalone watchdog");
    end
endmodule

`default_nettype wire
