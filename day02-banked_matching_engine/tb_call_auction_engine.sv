`timescale 1ns/1ps
`default_nettype none

module tb_call_auction_engine #(
    parameter integer PRICES = 16, SLOTS = 16, QTY_W = 16, OWNER_W = 8,
                      EPOCH_W = 16, TAG_W = 16
);
    localparam integer PW = $clog2(PRICES+1);
    localparam integer IW = $clog2(SLOTS+1);
    localparam integer SW = QTY_W+IW;
    localparam integer CW = 7+EPOCH_W+TAG_W+IW+OWNER_W+3*PW+QTY_W;
    localparam integer RW = 13+EPOCH_W+TAG_W+5*IW+2*OWNER_W+PW+3*SW;

    logic clk = 0;
    always #5 clk = !clk;
    logic rst = 1, s_valid = 0, m_ready = 0;
    wire s_ready, m_valid, m_last, m_side, m_market, m_carry, m_reference_half;
    wire [2:0] s_op;
    wire [3:0] m_kind, m_reason;
    wire [EPOCH_W-1:0] s_epoch, m_epoch;
    wire [TAG_W-1:0] s_tag, m_tag;
    wire [IW-1:0] s_id, m_first, m_second, m_count, m_carried, m_expired;
    wire [OWNER_W-1:0] s_owner, m_first_owner, m_second_owner;
    wire s_side, s_market, s_carry, s_reference_half;
    wire [PW-1:0] s_price, s_low, s_high, m_price;
    wire [QTY_W-1:0] s_quantity;
    wire [SW-1:0] m_quantity, m_buys, m_sells;
    logic [CW-1:0] stimulus;
    wire [RW-1:0] observed = {
        m_kind, m_reason, m_last, m_epoch, m_tag, m_first, m_second, m_first_owner, m_second_owner,
        m_side, m_market, m_carry, m_reference_half, m_price, m_quantity, m_buys, m_sells,
        m_count, m_carried, m_expired
    };
    assign {s_op, s_epoch, s_tag, s_id, s_owner, s_side, s_market, s_carry, s_reference_half,
            s_price, s_low, s_high, s_quantity} = stimulus;

    call_auction_engine #(
        .PRICES(PRICES), .SLOTS(SLOTS), .QTY_W(QTY_W), .OWNER_W(OWNER_W), .EPOCH_W(EPOCH_W), .TAG_W(TAG_W)
    ) dut (.*);

    integer tape, trace, row;
    integer cycle = 0, action, duration, idle = 0, hold = 0, tail = 0;
    logic pending = 0, finished = 0;
    logic [31:0] noise = 32'h9334ad78;
    string tape_path, trace_path, wave_path;

    always @(posedge clk) begin
        if (tape && trace && $time > 5) begin
            $fdisplay(trace, "%0d %b %b %b %b %b %h %h", cycle, rst, s_valid, s_ready, m_valid, m_ready,
                s_valid ? stimulus : '0, m_valid ? observed : '0);
            cycle <= cycle+1;
        end
    end

    initial begin
        if (!$value$plusargs("tape=%s", tape_path) || !$value$plusargs("trace=%s",trace_path))
            $fatal(1, "supply auction tape and capture paths");
        tape = $fopen(tape_path,"r");
        trace = $fopen(trace_path,"w");
        if (!tape || !trace)
            $fatal(1, "unable to open auction tape or capture");
        if ($value$plusargs("wave=%s",wave_path)) begin
            $dumpfile(wave_path);
            $dumpvars(1,tb_call_auction_engine);
        end
        repeat (2) @(negedge clk);
        while (tail < PRICES+4*SLOTS+32) begin
            rst = 0;
            if (!finished && !pending && idle == 0) begin
                row = $fscanf(tape,"%d %d %h",action,duration,stimulus);
                if (row == 3) begin
                    case (action)
                        0: pending = 1'b1;
                        1: idle = duration;
                        2: begin
                            rst = 1'b1;
                            idle = 2;
                            hold = 0;
                        end
                        3: hold = duration;
                        default: $fatal(1,"unexpected auction directive");
                    endcase
                end else if ($feof(tape)) begin
                    finished = 1'b1;
                end else begin
                    $fatal(1,"malformed auction row");
                end
            end
            s_valid = pending;
            m_ready = !rst && hold == 0 && (|noise[3:0]);
            noise = {noise[30:0],noise[31]^noise[28]^noise[5]^noise[0]};
            if (idle > 0) idle--;
            if (hold > 0) hold--;
            @(posedge clk);
            if (pending && s_ready) pending = 0;
            if (finished) tail++;
            if (cycle > 500000)
                $fatal(1,"auction failed to drain");
            @(negedge clk);
        end
        $fclose(tape);
        $fclose(trace);
        $display("Captured %0d auction cycles",cycle);
        $finish;
    end
endmodule

`default_nettype wire
