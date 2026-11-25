`timescale 1ns/1ps
`default_nettype none

// A frequency-only servo. The raw tick clock and accumulated phase continue
// independently. Accepted updates affect only future accumulator increments.
module latency_pps_discipline #(
    parameter integer REV_W = 16,
    parameter integer AUDIT_DEPTH = 8,
    parameter logic [63:0] INITIAL_PERIOD = 64'h0000000800000000,
    localparam integer OCC_W = $clog2(AUDIT_DEPTH+1)
) (
    input wire clk, rst,
    input wire [63:0] now_tick,

    input wire cfg_valid,
    output wire cfg_ready,
    input wire [1:0] cfg_op,
    input wire [REV_W-1:0] cfg_expected_rev,
    input wire cfg_enable,
    input wire [63:0] cfg_reference_ns, cfg_min_ticks, cfg_max_ticks, cfg_timeout,
    input wire [63:0] cfg_max_age, cfg_jitter, cfg_min_period, cfg_max_period, cfg_max_step,
    input wire [7:0] cfg_lock_windows,
    output wire rsp_valid,
    input wire rsp_ready,
    output logic [1:0] rsp_op,
    output logic [2:0] rsp_status,
    output logic [REV_W-1:0] rsp_rev,
    output logic [63:0] rsp_period,
    output logic [1:0] rsp_mode,

    input wire sample_valid,
    output wire sample_ready,
    input wire [63:0] sample_tick,
    output wire rate_valid,
    input wire rate_ready,
    output wire [63:0] rate_period,

    output wire audit_valid,
    input wire audit_ready,
    output wire [REV_W-1:0] audit_rev,
    output wire [63:0] audit_sequence,
    output wire [325:0] audit_data,
    output wire [OCC_W-1:0] audit_occupancy,
    output logic [63:0] audit_cursor,

    output logic [1:0] mode,
    output logic anchor_valid,
    output logic [63:0] anchor_tick, active_period,
    output logic [1:0] interval_count,
    output logic [7:0] stable_windows,
    output logic [31:0] sampled, invalid_samples, usable_intervals, applied_rates,
    output logic [31:0] rejected_periods, holdovers, lost_audits
);
    localparam logic [1:0] PROGRAM = 0, INSPECT = 1, RESEED = 2, CLEAR = 3;
    localparam logic [2:0] OK = 0, STALE_CONFIG = 1, BAD_POLICY = 2, EXHAUSTED = 3;
    localparam logic [1:0] OFF = 0, ACQUIRE = 1, LOCKED = 2, HOLDOVER = 3;
    localparam logic [3:0] DISABLED = 0, STALE_SAMPLE = 1, SEED = 2, REVERSE = 3,
                           EARLY = 4, LATE = 5, WARMUP = 6, JITTER = 7,
                           QUORUM = 8, MEASURE = 9, RATE_RANGE = 10, UNCHANGED = 11,
                           APPLIED = 12, ABANDONED = 13;

    logic enabled, reply_pending, math_pending, rate_pending;
    wire audit_terminal = &audit_cursor;
    logic [REV_W-1:0] revision;
    logic [63:0] reference_ns, min_ticks, max_ticks, timeout_ticks, maximum_age;
    logic [63:0] jitter, min_period, max_period, max_step;
    logic [7:0] quorum;
    logic [63:0] recent, older, measurement_tick, measurement_median;
    logic [63:0] proposed_rate, limited_rate;
    logic [2:0] config_status;
    logic [3:0] sample_status;

    wire mutation = cfg_op == PROGRAM || cfg_op == RESEED;
    wire valid_policy = cfg_reference_ns != 0 && cfg_min_ticks != 0 &&
                        cfg_min_ticks <= cfg_max_ticks && !cfg_max_ticks[63] &&
                        cfg_timeout > cfg_max_ticks && !cfg_timeout[63] &&
                        cfg_max_age <= cfg_timeout &&
                        cfg_min_period != 0 && cfg_min_period <= active_period &&
                        active_period <= cfg_max_period &&
                        cfg_max_step != 0 && cfg_lock_windows != 0;
    assign cfg_ready = !rst && (!reply_pending || rsp_ready) &&
                       (!mutation || (!math_pending && !rate_pending));
    assign rsp_valid = !rst && reply_pending;
    wire cfg_accept = cfg_valid && cfg_ready;
    wire program_policy = cfg_accept && cfg_op == PROGRAM && config_status == OK;
    wire boundary = cfg_accept && mutation && config_status == OK;
    wire clear_stats = cfg_accept && cfg_op == CLEAR && config_status == OK;

    always_comb begin
        config_status = OK;
        if (cfg_op != INSPECT && cfg_expected_rev != revision)
            config_status = STALE_CONFIG;
        else if (mutation && (&revision))
            config_status = EXHAUSTED;
        else if (cfg_op == PROGRAM && cfg_enable && !valid_policy)
            config_status = BAD_POLICY;
    end

    wire [63:0] age = now_tick - sample_tick;
    wire [63:0] gap = sample_tick - anchor_tick;
    wire [63:0] anchor_age = now_tick - anchor_tick;
    wire [63:0] pair_low = recent < older ? recent : older;
    wire [63:0] pair_high = recent < older ? older : recent;
    wire [63:0] interval_low = gap < pair_low ? gap : pair_low;
    wire [63:0] interval_high = gap > pair_high ? gap : pair_high;
    wire [63:0] median = gap <= pair_low ? pair_low : gap >= pair_high ? pair_high : gap;
    wire [7:0] next_streak = (&stable_windows) ? stable_windows : stable_windows + 1'b1;

    always @* begin
        if (!enabled)
            sample_status = DISABLED;
        else if (age[63] || age > maximum_age)
            sample_status = STALE_SAMPLE;
        else if (!anchor_valid)
            sample_status = SEED;
        else if (gap == 0 || gap[63])
            sample_status = REVERSE;
        else if (gap < min_ticks)
            sample_status = EARLY;
        else if (gap > max_ticks)
            sample_status = LATE;
        else if (interval_count < 2)
            sample_status = WARMUP;
        else if (interval_high - interval_low > jitter)
            sample_status = JITTER;
        else if (next_streak < quorum)
            sample_status = QUORUM;
        else
            sample_status = MEASURE;
    end

    wire divide_ready, divide_valid, divide_overflow, divide_zero;
    wire [63:0] estimate;
    assign sample_ready = !rst && !boundary && !math_pending && !rate_pending && divide_ready;
    wire sample_accept = sample_valid && sample_ready;
    wire start_math = sample_accept && sample_status == MEASURE;
    wire use_interval = sample_accept && sample_status >= WARMUP;
    wire replace_anchor = sample_accept && (sample_status == SEED || sample_status == LATE);
    wire watchdog = enabled && anchor_valid && !boundary && !use_interval &&
                    !replace_anchor && (anchor_age[63] || anchor_age >= timeout_ticks);
    wire math_done = divide_valid && math_pending;
    wire math_stale = !anchor_valid || watchdog;
    wire value_bad = divide_overflow || divide_zero || estimate < min_period || estimate > max_period;
    wire math_keep = math_done && !math_stale && !value_bad && estimate == active_period;
    wire offer_rate = math_done && !math_stale && !value_bad && estimate != active_period;
    wire rate_accept = rate_valid && rate_ready;
    assign rate_valid = !rst && rate_pending;
    assign rate_period = rate_valid ? limited_rate : '0;

    wire [63:0] upward = estimate - active_period;
    wire [63:0] downward = active_period - estimate;
    wire [63:0] bounded_estimate =
        estimate > active_period && upward > max_step ? active_period + max_step :
        estimate < active_period && downward > max_step ? active_period - max_step : estimate;

    latency_period_divide divider (
        .clk, .rst, .s_valid(start_math), .s_ready(divide_ready),
        .s_reference_ns(reference_ns), .s_ticks(median),
        .m_valid(divide_valid), .m_ready(math_pending), .m_period(estimate),
        .m_overflow(divide_overflow), .m_zero_ticks(divide_zero)
    );

    wire audit_available;
    wire report_math = math_done && !offer_rate;
    wire trace = sample_accept || report_math || rate_accept;
    wire [3:0] trace_code = sample_accept ? sample_status :
                           rate_accept ? APPLIED :
                           math_stale ? ABANDONED : value_bad ? RATE_RANGE : UNCHANGED;
    wire [63:0] trace_tick = rate_accept ? now_tick + 1'b1 : now_tick;
    wire [63:0] trace_sample = sample_accept ? sample_tick : measurement_tick;
    wire [63:0] trace_ticks = sample_accept ?
                             (anchor_valid && sample_status >= REVERSE ? gap : 64'd0) :
                             measurement_median;
    wire [63:0] trace_target = sample_accept ? 64'd0 : rate_accept ? proposed_rate : estimate;
    wire [63:0] trace_rate = rate_accept ? limited_rate : active_period;
    wire [325:0] trace_word = {
        trace_code,mode,trace_tick,trace_sample,trace_ticks,trace_target,trace_rate
    };

    latency_alert_fifo #(.WIDTH(64+REV_W+326), .DEPTH(AUDIT_DEPTH)) audit_queue (
        .clk, .rst, .s_valid(trace && !audit_terminal), .s_ready(audit_available),
        .s_data({audit_cursor,revision,trace_word}), .m_valid(audit_valid), .m_ready(audit_ready),
        .m_data({audit_sequence,audit_rev,audit_data}), .used(audit_occupancy)
    );

    function automatic logic [31:0] accumulate(input logic [31:0] count,
                                                input logic reset_count, step);
        logic [31:0] base;
        begin
            base = reset_count ? '0 : count;
            accumulate = step && !(&base) ? base + 1'b1 : base;
        end
    endfunction

    always_ff @(posedge clk) begin
        if (rst) begin
            reply_pending <= 0;
            rsp_op <= '0;
            rsp_status <= OK;
            rsp_rev <= '0;
            rsp_period <= INITIAL_PERIOD;
            rsp_mode <= OFF;
            enabled <= 0;
            revision <= '0;
            reference_ns <= '0;
            min_ticks <= '0;
            max_ticks <= '0;
            timeout_ticks <= '0;
            maximum_age <= '0;
            jitter <= '0;
            min_period <= '0;
            max_period <= '0;
            max_step <= '0;
            quorum <= '0;
        end else begin
            if (rsp_ready)
                reply_pending <= 0;
            if (cfg_accept) begin
                reply_pending <= 1;
                rsp_op <= cfg_op;
                rsp_status <= config_status;
                rsp_rev <= revision + REV_W'(boundary);
                rsp_period <= active_period;
                rsp_mode <= boundary ? ((program_policy ? cfg_enable : enabled) ? ACQUIRE : OFF) : mode;
            end
            if (boundary)
                revision <= revision + 1'b1;
            if (program_policy) begin
                enabled <= cfg_enable;
                reference_ns <= cfg_reference_ns;
                min_ticks <= cfg_min_ticks;
                max_ticks <= cfg_max_ticks;
                timeout_ticks <= cfg_timeout;
                maximum_age <= cfg_max_age;
                jitter <= cfg_jitter;
                min_period <= cfg_min_period;
                max_period <= cfg_max_period;
                max_step <= cfg_max_step;
                quorum <= cfg_lock_windows;
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            mode <= OFF;
            anchor_valid <= 0;
            anchor_tick <= '0;
            interval_count <= '0;
            stable_windows <= '0;
            recent <= '0;
            older <= '0;
        end else if (boundary) begin
            mode <= (program_policy ? cfg_enable : enabled) ? ACQUIRE : OFF;
            anchor_valid <= 0;
            anchor_tick <= '0;
            interval_count <= '0;
            stable_windows <= '0;
            recent <= '0;
            older <= '0;
        end else if (watchdog) begin
            mode <= HOLDOVER;
            anchor_valid <= 0;
            interval_count <= '0;
            stable_windows <= '0;
        end else begin
            if (replace_anchor) begin
                anchor_valid <= 1;
                anchor_tick <= sample_tick;
                interval_count <= '0;
                stable_windows <= '0;
                if (mode != HOLDOVER)
                    mode <= ACQUIRE;
            end else if (use_interval) begin
                anchor_tick <= sample_tick;
                older <= recent;
                recent <= gap;
                if (interval_count < 3)
                    interval_count <= interval_count + 1'b1;
                if (sample_status == JITTER) begin
                    stable_windows <= '0;
                    if (mode != HOLDOVER)
                        mode <= ACQUIRE;
                end else if (sample_status >= QUORUM) begin
                    stable_windows <= next_streak;
                end
            end
            if (math_done && !math_stale && value_bad) begin
                stable_windows <= '0;
                if (mode != HOLDOVER)
                    mode <= ACQUIRE;
            end
            if (math_keep || (rate_accept && anchor_valid))
                mode <= LOCKED;
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            math_pending <= 0;
            rate_pending <= 0;
            measurement_tick <= '0;
            measurement_median <= '0;
            active_period <= INITIAL_PERIOD;
            proposed_rate <= '0;
            limited_rate <= '0;
            sampled <= '0;
            invalid_samples <= '0;
            usable_intervals <= '0;
            applied_rates <= '0;
            rejected_periods <= '0;
            holdovers <= '0;
            lost_audits <= '0;
            audit_cursor <= '0;
        end else begin
            if (start_math) begin
                math_pending <= 1;
                measurement_tick <= sample_tick;
                measurement_median <= median;
            end else if (math_done) begin
                math_pending <= 0;
            end
            if (offer_rate) begin
                rate_pending <= 1;
                proposed_rate <= estimate;
                limited_rate <= bounded_estimate;
            end else if (rate_accept) begin
                rate_pending <= 0;
                active_period <= limited_rate;
            end
            sampled <= accumulate(sampled,clear_stats,sample_accept);
            invalid_samples <= accumulate(invalid_samples,clear_stats,sample_accept &&
                               sample_status < WARMUP && sample_status != SEED);
            usable_intervals <= accumulate(usable_intervals,clear_stats,use_interval);
            applied_rates <= accumulate(applied_rates,clear_stats,rate_accept);
            rejected_periods <= accumulate(rejected_periods,clear_stats,report_math &&
                                           (math_stale || value_bad));
            holdovers <= accumulate(holdovers,clear_stats,watchdog);
            if (trace && !audit_terminal)
                audit_cursor <= audit_cursor + 1'b1;
            lost_audits <= accumulate(lost_audits,clear_stats,trace &&
                                     (!audit_available || audit_terminal));
        end
    end
endmodule

`default_nettype wire
