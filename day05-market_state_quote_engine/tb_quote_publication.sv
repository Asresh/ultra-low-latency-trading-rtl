`timescale 1ns/1ps
`default_nettype none

module tb_quote_publication;
    localparam integer BANKS = 3, EW = 4, RW = 5, PW = 5, QW = 7;
    localparam integer BW = $clog2(BANKS+1);
    localparam integer KW = 1+EW+2*PW+QW;
    localparam integer RANDOM_STEPS = 2400;

    logic clk = 1'b0;
    always #5 clk = !clk;
    logic rst = 1'b1;
    logic q_ready = 1'b0;
    logic [BANKS*KW-1:0] desired = '0;
    logic [BANKS*3-1:0] desired_reason = '0;
    wire q_valid, q_active;
    wire [BW-1:0] q_instrument;
    wire [EW-1:0] q_epoch;
    wire [RW-1:0] q_revision;
    wire [PW-1:0] q_bid, q_ask;
    wire [QW-1:0] q_size;
    wire [2:0] q_reason;

    quote_publication #(.BANKS(BANKS), .PRICE_W(PW), .QTY_W(QW), .EPOCH_W(EW), .REV_W(RW))
        dut (.*);

    logic [KW-1:0] model_last [0:BANKS-1];
    integer model_revision [0:BANKS-1];
    integer turn, present_bank, present_rev;
    logic [KW-1:0] present_key;
    logic [2:0] present_reason;
    integer transfers [0:BANKS-1], wraps [0:BANKS-1], eligible_wait [0:BANKS-1];
    integer blocked_cycles, withdrawals, dirty_changes_on_hold, max_wait, resets_with_pending;
    logic [31:0] rng = 32'h071f_92c5;

    function automatic integer dice(input integer modulus);
        rng = rng ^ (rng << 13);
        rng = rng ^ (rng >> 17);
        rng = rng ^ (rng << 5);
        dice = int'(rng % $unsigned(modulus));
    endfunction

    function automatic bit dirty(input integer bank);
        logic [KW-1:0] value;
        value = desired[bank*KW+:KW];
        dirty = (value[KW-1] || model_last[bank][KW-1]) && value != model_last[bank];
    endfunction

    task automatic oracle_reset;
        turn = 0;
        present_bank = -1;
        present_rev = 0;
        present_reason = '0;
        present_key = '0;
        for (integer bank = 0; bank < BANKS; bank++) begin
            model_last[bank] = '0;
            model_revision[bank] = 0;
            eligible_wait[bank] = 0;
        end
    endtask

    task automatic update_intent(
        input integer bank, input bit active, input integer epoch, input integer variation
    );
        if (active) begin
            desired[bank*KW+:KW] = {1'b1, EW'(epoch), PW'(bank+variation%7),
                 PW'(bank+variation%7+5), QW'(1+variation%31)};
            desired_reason[bank*3+:3] = 0;
        end else begin
            desired[bank*KW+:KW] = {1'b0, EW'(epoch), PW'(0), PW'(0), QW'(0)};
            desired_reason[bank*3+:3] = 3'(1+variation%5);
        end
    endtask

    task automatic check_and_advance(input integer step);
        integer next_bank, probe;
        if (rst) begin
            if (q_valid)
                $fatal(1, "publication held valid through reset");
            if (present_bank >= 0)
                resets_with_pending++;
            oracle_reset();
            return;
        end
        if (q_valid !== (present_bank >= 0))
            $fatal(1, "bad publisher valid at step %0d", step);
        if (present_bank >= 0) begin
            if ({q_instrument, q_active, q_epoch, q_bid, q_ask, q_size, q_reason, q_revision}
                !== {BW'(present_bank), present_key, present_reason, RW'(present_rev)})
                $fatal(1, "wrong immutable quote snapshot at step %0d", step);
            if (!q_ready) begin
                blocked_cycles++;
                for (integer bank = 0; bank < BANKS; bank++)
                    if (dirty(bank))
                        dirty_changes_on_hold++;
                return;
            end
            transfers[present_bank]++;
            if (!present_key[KW-1])
                withdrawals++;
        end
        next_bank = -1;
        for (integer offset = 0; offset < BANKS; offset++) begin
            probe = (turn+offset) % BANKS;
            if (next_bank < 0 && dirty(probe))
                next_bank = probe;
        end
        present_bank = next_bank;
        for (integer bank = 0; bank < BANKS; bank++) begin
            if (next_bank == bank || !dirty(bank)) begin
                eligible_wait[bank] = 0;
            end else if (next_bank >= 0) begin
                eligible_wait[bank]++;
                if (eligible_wait[bank] > max_wait)
                    max_wait = eligible_wait[bank];
                if (eligible_wait[bank] >= BANKS)
                    $fatal(1, "publisher violated bounded rotation");
            end
        end
        if (next_bank >= 0) begin
            present_key = desired[next_bank*KW+:KW];
            present_reason = desired_reason[next_bank*3+:3];
            present_rev = model_revision[next_bank];
            model_last[next_bank] = present_key;
            model_revision[next_bank] = (model_revision[next_bank]+1) % (1<<RW);
            if (model_revision[next_bank] == 0)
                wraps[next_bank]++;
            turn = (next_bank+1) % BANKS;
        end
    endtask

    initial begin : drive
        integer action, lane;
        oracle_reset();
        blocked_cycles = 0;
        withdrawals = 0;
        dirty_changes_on_hold = 0;
        max_wait = 0;
        resets_with_pending = 0;
        for (integer bank = 0; bank < BANKS; bank++) begin
            transfers[bank] = 0;
            wraps[bank] = 0;
        end
        for (integer step = 0; step < RANDOM_STEPS+16; step++) begin
            @(negedge clk);
            rst = (step < 2 || step == 575 || step == 1388);
            q_ready = (step >= RANDOM_STEPS) ||
                (step > 25 && step % 137 < 99 && dice(7) != 0);
            if (step < 65) begin
                for (integer bank = 0; bank < BANKS; bank++)
                    update_intent(bank, 1'b1, step/4, step+bank);
            end else if (step < RANDOM_STEPS) begin
                action = dice(5);
                lane = dice(BANKS);
                if (action > 0)
                    update_intent(lane, action != 1, dice(1<<EW), dice(511));
                if (step % 29 == 0)
                    update_intent((lane+1)%BANKS, 1'b0, dice(1<<EW), dice(97));
            end
            @(posedge clk);
            check_and_advance(step);
        end
        if (present_bank >= 0)
            $fatal(1, "publisher did not drain stable targets");
        for (integer bank = 0; bank < BANKS; bank++) begin
            if (dirty(bank) || transfers[bank] < 40 || wraps[bank] == 0)
                $fatal(1, "bank %0d failed convergence or revision-wrap coverage", bank);
        end
        if (blocked_cycles == 0 || withdrawals == 0 || dirty_changes_on_hold == 0 ||
            max_wait != BANKS-1 || resets_with_pending == 0)
            $fatal(1, "missing publication coverage");
        $display("PASS publication banks=%0d stalls=%0d withdrawals=%0d dirty_while_held=%0d maxwait=%0d",
            BANKS, blocked_cycles, withdrawals, dirty_changes_on_hold, max_wait);
        $finish;
    end
endmodule

`default_nettype wire
