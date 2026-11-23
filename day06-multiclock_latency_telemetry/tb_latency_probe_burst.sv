`timescale 1ns/1ps

module tb_latency_probe_burst;
    localparam integer N = 4;
    localparam integer TW = 12;
    localparam integer GROUP_W = 2;
    localparam integer ID_W = 2;
    localparam integer EPOCH_W = 8;
    localparam integer W = EPOCH_W + TW + GROUP_W + ID_W + 65;
    localparam integer ITEMS = 900;
    logic manager_clk = 0;
    always #5 manager_clk = !manager_clk;
    logic [N-1:0] clocks = 0;
    always #3 clocks[0] = !clocks[0];
    always #7 clocks[1] = !clocks[1];
    always #8 clocks[2] = !clocks[2];
    always #13 clocks[3] = !clocks[3];

    logic power_reset = 0;
    wire manager_reset;
    wire [N-1:0] probe_reset;
    latency_reset_sync rs(.clk(manager_clk), .power_reset, .reset(manager_reset));
    logic [63:0] manager_tick = 0;
    logic [63:0] published_gray = 0;
    logic [N-1:0] sv = 0;
    wire [N-1:0] sr, xv, xr;
    logic [N*TW-1:0] tags = 0;
    logic [N*GROUP_W-1:0] groups = 0;
    logic [N-1:0] ending = 0;
    wire [N*W-1:0] crossed_data;
    logic running = 0, m_ready = 0;
    wire m_valid;
    wire [W-1:0] m_data;
    integer accepted [0:N-1], drained [0:N-1], blocked [0:N-1];
    logic [63:0] captured_tick [0:N-1][0:ITEMS-1];

    always @(posedge manager_clk) begin
        if (manager_reset) begin
            manager_tick <= 0;
            published_gray <= 0;
        end else begin
            manager_tick <= manager_tick + 1;
            published_gray <= (manager_tick + 1) ^ ((manager_tick + 1) >> 1);
        end
    end

    // Generate a reproducible tag that distinguishes probes and event indices.
    function automatic logic [TW-1:0] tag_for(input integer lane, input integer index);
        tag_for = TW'((index * 37) ^ (lane * 623));
    endfunction

    // Assign a probe event to its deterministic test group.
    function automatic logic [GROUP_W-1:0] group_for(input integer index);
        group_for = GROUP_W'(index ^ (index >> 4));
    endfunction

    for (genvar lane = 0; lane < N; lane++) begin : g
        latency_reset_sync r(.clk(clocks[lane]), .power_reset, .reset(probe_reset[lane]));
        latency_probe #(
            .TAG_W(TW), .CLASS_W(GROUP_W), .PROBE_W(ID_W), .PROBE_NUMBER(lane),
            .EPOCH_W(EPOCH_W), .FIFO_ADDRESS_W(3)
        ) p (
            .clk(clocks[lane]), .reset(probe_reset[lane]),
            .manager_clk, .manager_reset, .gray_ticks_async(published_gray),
            .epoch_gray_async(8'h2a), .valid(sv[lane]), .ready(sr[lane]),
            .tag(tags[lane*TW+:TW]), .event_class(groups[lane*GROUP_W+:GROUP_W]),
            .end_event(ending[lane]), .manager_valid(xv[lane]), .manager_ready(xr[lane]),
            .observation(crossed_data[lane*W+:W])
        );

        always @(negedge clocks[lane]) begin
            if (running && accepted[lane] < ITEMS) begin
                sv[lane] = 1;
                tags[lane*TW+:TW] = tag_for(lane, accepted[lane]);
                groups[lane*GROUP_W+:GROUP_W] = group_for(accepted[lane]);
                ending[lane] = 1'(accepted[lane] + lane);
            end else begin
                sv[lane] = 0;
            end
        end

        always @(posedge clocks[lane]) begin
            if (probe_reset[lane]) begin
                accepted[lane] <= 0;
                blocked[lane] <= 0;
            end else begin
                if (sv[lane] && sr[lane]) begin
                    captured_tick[lane][accepted[lane]] <= manager_tick;
                    accepted[lane] <= accepted[lane] + 1;
                end
                if (sv[lane] && !sr[lane])
                    blocked[lane] <= blocked[lane] + 1;
            end
        end
    end

    latency_rr #(.PORTS(N), .WIDTH(W)) merge(
        .clk(manager_clk), .reset(manager_reset), .s_valid(xv), .s_ready(xr),
        .s_data(crossed_data), .m_valid, .m_ready, .m_data
    );
    wire [EPOCH_W-1:0] out_epoch;
    wire out_end;
    wire [GROUP_W-1:0] out_class;
    wire [TW-1:0] out_tag;
    wire [ID_W-1:0] out_lane;
    wire [63:0] out_tick;
    assign {out_epoch,out_end,out_class,out_tag,out_lane,out_tick} = m_data;

    logic last_stalled = 0;
    logic [W-1:0] held;
    logic [31:0] random_ready = 32'h14894978;
    integer transferred = 0, stalls = 0, simultaneous = 0, delayed_cycles = 0;
    integer drainer_ticks = 0;
    logic enable_sink = 0;
    logic [63:0] max_probe_lag [0:N-1];
    logic [63:0] last_source_tick [0:N-1];

    always @(negedge manager_clk) begin
        random_ready = {random_ready[30:0],random_ready[31]^random_ready[21]^random_ready[1]^random_ready[0]};
        m_ready = enable_sink && random_ready[3:0] > 4 && (drainer_ticks % 108) > 17;
    end

    always @(posedge manager_clk) begin
        if (manager_reset) begin
            for (integer lane = 0; lane < N; lane++) begin
                drained[lane] = 0;
                max_probe_lag[lane] = 0;
                last_source_tick[lane] = 0;
            end
            transferred = 0;
            last_stalled = 0;
            drainer_ticks = 0;
        end else begin
            if (running) drainer_ticks++;
            if (last_stalled && (!m_valid || m_data !== held))
                $fatal(1, "Arbiter changed lane under stall");
            last_stalled = m_valid && !m_ready;
            if (last_stalled) begin
                held = m_data;
                stalls++;
            end
            if ($countones(xv) >= 3)
                simultaneous++;
            if (m_valid && m_ready) begin
                if (drained[out_lane] >= accepted[out_lane])
                    $fatal(1, "Output precedes producer");
                if (out_tag !== tag_for(int'(out_lane),drained[out_lane]) ||
                    out_class !== group_for(drained[out_lane]) ||
                    out_end !== 1'(drained[out_lane] + int'(out_lane)) ||
                    out_epoch !== 8'h2a)
                    $fatal(1, "Crossed metadata corrupt at lane %0d index %0d", out_lane, drained[out_lane]);
                if (out_tick < last_source_tick[out_lane] ||
                    out_tick > captured_tick[out_lane][drained[out_lane]])
                    $fatal(1, "Raw tick estimates must be monotonic and not in the future");
                if (captured_tick[out_lane][drained[out_lane]] - out_tick > 12)
                    $fatal(1, "CDC timestamp lag outside simulated bound");
                if (captured_tick[out_lane][drained[out_lane]] - out_tick > max_probe_lag[out_lane])
                    max_probe_lag[out_lane] = captured_tick[out_lane][drained[out_lane]] - out_tick;
                if (manager_tick > captured_tick[out_lane][drained[out_lane]] + 15)
                    delayed_cycles++;
                last_source_tick[out_lane] = out_tick;
                drained[out_lane]++;
                transferred++;
            end
        end
    end

    initial begin
        #1 power_reset = 1;
        repeat (21) @(negedge manager_clk);
        power_reset = 0;
        wait (&sr);
        repeat (8) @(negedge manager_clk);
        running = 1;
        repeat (36) @(negedge manager_clk);
        enable_sink = 1;
        wait (transferred == ITEMS * N);
        repeat (35) @(negedge manager_clk);
        if (m_valid || xv != 0 || delayed_cycles == 0 || stalls == 0 || simultaneous == 0)
            $fatal(1, "Drain or contention coverage missing");
        for (integer lane = 0; lane < N; lane++)
            if (drained[lane] != ITEMS || blocked[lane] < 5 || max_probe_lag[lane] == 0)
                $fatal(1, "Lane %0d was not exercised", lane);
        $display("PASS multiclock burst: %0d observations; %0d held, %0d multi-lane cycles",
                 transferred, stalls, simultaneous);
        $display("Max simulated probe lag %0d/%0d/%0d/%0d ticks; %0d records spent >15 clocks queued",
                 max_probe_lag[0],max_probe_lag[1],max_probe_lag[2],max_probe_lag[3],delayed_cycles);
        $finish;
    end
    initial begin #280000; $fatal(1,"probe stress timed out"); end
endmodule
