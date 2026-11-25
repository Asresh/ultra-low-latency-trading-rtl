`timescale 1ns/1ps
`default_nettype none

// Clock this tap in the telemetry manager's reference domain and feed each
// accepted report once. Alert backpressure never delays the primary report.
module latency_deadline_monitor #(
    parameter integer RULES = 4,
    parameter integer ALERT_DEPTH = 8,
    parameter integer REV_W = 16,
    localparam integer KEY_W = $clog2(RULES+1),
    localparam integer RULE_W = RULES > 1 ? $clog2(RULES) : 1,
    localparam integer OCC_W = $clog2(ALERT_DEPTH+1)
) (
    input  wire clk,
    input  wire rst,
    input  wire sample_valid,
    input  wire [319:0] sample_data,
    input  wire ctl_valid,
    output wire ctl_ready,
    input  wire [1:0] ctl_op,
    input  wire [KEY_W-1:0] ctl_rule,
    input  wire [REV_W-1:0] ctl_expected_revision,
    input  wire ctl_enable,
    input  wire [7:0] ctl_class, ctl_start, ctl_stop,
    input  wire [2:0] ctl_wild,
    input  wire [63:0] ctl_limit,
    input  wire [15:0] ctl_trip, ctl_recover,
    output wire reply_valid,
    input  wire reply_ready,
    output logic [2:0] reply_status,
    output logic [1:0] reply_op,
    output logic [KEY_W-1:0] reply_rule,
    output logic [REV_W-1:0] reply_revision,
    output logic [380:0] reply_data,
    output wire alert_valid,
    input  wire alert_ready,
    output wire [RULE_W-1:0] alert_rule,
    output wire [REV_W-1:0] alert_revision,
    output wire [255:0] alert_data,
    output wire [OCC_W-1:0] alert_occupancy,
    output logic [31:0] seen_reports, nonmatches, unclassified, lost_alerts
);
    localparam logic [1:0] PROGRAM = 0, INSPECT = 1, CLEAR = 2;
    localparam logic [2:0] DONE = 0, BAD_RULE = 1, STALE = 2, BAD_POLICY = 3,
                           EXHAUSTED = 4, BAD_OPERATION = 5;
    localparam logic [7:0] ENTER_ALARM = 1, LEAVE_ALARM = 2;

    logic [RULES-1:0] enabled, alarmed;
    logic [7:0] classes [0:RULES-1], start_probes [0:RULES-1], stop_probes [0:RULES-1];
    logic [2:0] wild [0:RULES-1];
    logic [63:0] limits [0:RULES-1], worst [0:RULES-1];
    logic [15:0] trips [0:RULES-1], recoveries [0:RULES-1];
    logic [15:0] bad_streak [0:RULES-1], good_streak [0:RULES-1];
    logic [31:0] matched [0:RULES-1], breaches [0:RULES-1];
    logic [31:0] rising [0:RULES-1], falling [0:RULES-1], losses [0:RULES-1];
    logic [REV_W-1:0] revision [0:RULES-1];
    logic pending_reply;

    wire [7:0] sample_class = sample_data[316:309];
    wire [15:0] sample_tag = sample_data[308:293];
    wire [7:0] sample_start = sample_data[292:285];
    wire [7:0] sample_stop = sample_data[284:277];
    wire [63:0] delta = sample_data[127:64];
    wire [148:0] unused_sample_fields = sample_data[276:128];
    wire match_report = sample_valid && sample_data[319:317] == 3'd1;
    wire [RULES-1:0] applicable;
    logic found;
    logic [RULE_W-1:0] first_rule;

    wire legal_rule = int'(ctl_rule) < RULES;
    wire [RULE_W-1:0] control_index = legal_rule ? RULE_W'(ctl_rule) : '0;
    wire revision_match = revision[control_index] == ctl_expected_revision;
    logic [2:0] result;
    assign reply_valid = !rst && pending_reply;
    assign ctl_ready = !rst && (!pending_reply || reply_ready);
    wire command = ctl_valid && ctl_ready;
    wire program_rule = command && result == DONE && ctl_op == PROGRAM;
    wire clear_rule = command && result == DONE && ctl_op == CLEAR;

    always_comb begin
        result = DONE;
        if (!legal_rule)
            result = BAD_RULE;
        else if (ctl_op > CLEAR)
            result = BAD_OPERATION;
        else if (ctl_op != INSPECT && !revision_match)
            result = STALE;
        else if (ctl_op == PROGRAM && revision[control_index] == '1)
            result = EXHAUSTED;
        else if (ctl_op == PROGRAM && ctl_enable && (ctl_trip == 0 || ctl_recover == 0))
            result = BAD_POLICY;
    end

    for (genvar rule = 0; rule < RULES; rule++) begin : select_rules
        assign applicable[rule] = enabled[rule] &&
            (wild[rule][2] || classes[rule] == sample_class) &&
            (wild[rule][1] || start_probes[rule] == sample_start) &&
            (wild[rule][0] || stop_probes[rule] == sample_stop);
    end

    always_comb begin
        found = 0;
        first_rule = '0;
        for (integer index = 0; index < RULES; index++) begin
            if (!found && applicable[index]) begin
                found = 1;
                first_rule = RULE_W'(index);
            end
        end
    end

    function automatic logic [15:0] inc16(input logic [15:0] count);
        inc16 = count == '1 ? count : count + 1'b1;
    endfunction

    function automatic logic [31:0] count32(input logic [31:0] count, input logic clear, step);
        logic [31:0] base;
        begin
            base = clear ? '0 : count;
            count32 = step && base != '1 ? base + 1'b1 : base;
        end
    endfunction

    wire classified = !rst && match_report && found;
    wire bad = delta > limits[first_rule];
    wire [15:0] bad_next = inc16(bad_streak[first_rule]);
    wire [15:0] good_next = inc16(good_streak[first_rule]);
    wire enter_alarm = classified && bad && !alarmed[first_rule] &&
                       bad_next >= trips[first_rule];
    wire leave_alarm = classified && !bad && alarmed[first_rule] &&
                       good_next >= recoveries[first_rule];
    wire transition = enter_alarm || leave_alarm;
    wire fifo_ready;
    wire lost = transition && !fifo_ready;
    wire [255:0] alert_word = {
        sample_class,sample_tag,sample_start,sample_stop,enter_alarm ? ENTER_ALARM : LEAVE_ALARM,
        enter_alarm ? bad_next : good_next,limits[first_rule],delta,sample_data[63:0]
    };

    latency_alert_fifo #(.WIDTH(RULE_W+REV_W+256), .DEPTH(ALERT_DEPTH)) alerts (
        .clk, .rst, .s_valid(transition), .s_ready(fifo_ready),
        .s_data({first_rule,revision[first_rule],alert_word}),
        .m_valid(alert_valid), .m_ready(alert_ready),
        .m_data({alert_rule,alert_revision,alert_data}), .used(alert_occupancy)
    );

    always_ff @(posedge clk) begin
        if (rst) begin
            pending_reply <= 0;
            reply_status <= DONE;
            reply_op <= '0;
            reply_rule <= '0;
            reply_revision <= '0;
            reply_data <= '0;
            seen_reports <= '0;
            nonmatches <= '0;
            unclassified <= '0;
            lost_alerts <= '0;
        end else begin
            if (ctl_ready)
                pending_reply <= ctl_valid;
            if (command) begin
                reply_status <= result;
                reply_op <= ctl_op;
                reply_rule <= ctl_rule;
                reply_revision <= !legal_rule ? '0 :
                                  revision[control_index] + REV_W'(program_rule);
                reply_data <= legal_rule ? {
                    enabled[control_index],classes[control_index],start_probes[control_index],
                    stop_probes[control_index],wild[control_index],alarmed[control_index],
                    limits[control_index],trips[control_index],recoveries[control_index],
                    bad_streak[control_index],good_streak[control_index],matched[control_index],
                    breaches[control_index],rising[control_index],falling[control_index],
                    losses[control_index],worst[control_index]
                } : '0;
            end
            seen_reports <= count32(seen_reports,0,sample_valid);
            nonmatches <= count32(nonmatches,0,sample_valid && !match_report);
            unclassified <= count32(unclassified,0,match_report && !found);
            lost_alerts <= count32(lost_alerts,0,lost);
        end
    end

    for (genvar rule = 0; rule < RULES; rule++) begin : state
        wire hit = classified && first_rule == RULE_W'(rule);
        wire update = program_rule && control_index == RULE_W'(rule);
        wire clear = clear_rule && control_index == RULE_W'(rule);
        always_ff @(posedge clk) begin
            if (rst) begin
                enabled[rule] <= 0;
                alarmed[rule] <= 0;
                classes[rule] <= '0;
                start_probes[rule] <= '0;
                stop_probes[rule] <= '0;
                wild[rule] <= '0;
                limits[rule] <= '0;
                trips[rule] <= '0;
                recoveries[rule] <= '0;
                revision[rule] <= '0;
                bad_streak[rule] <= '0;
                good_streak[rule] <= '0;
                worst[rule] <= '0;
                matched[rule] <= '0;
                breaches[rule] <= '0;
                rising[rule] <= '0;
                falling[rule] <= '0;
                losses[rule] <= '0;
            end else begin
                if (hit) begin
                    alarmed[rule] <= enter_alarm || (alarmed[rule] && !leave_alarm);
                    bad_streak[rule] <= bad ? bad_next : '0;
                    good_streak[rule] <= bad ? '0 : good_next;
                end
                if (update) begin
                    enabled[rule] <= ctl_enable;
                    classes[rule] <= ctl_class;
                    start_probes[rule] <= ctl_start;
                    stop_probes[rule] <= ctl_stop;
                    wild[rule] <= ctl_wild;
                    limits[rule] <= ctl_limit;
                    trips[rule] <= ctl_trip;
                    recoveries[rule] <= ctl_recover;
                    revision[rule] <= revision[rule] + 1'b1;
                    alarmed[rule] <= 0;
                    bad_streak[rule] <= '0;
                    good_streak[rule] <= '0;
                end
                matched[rule] <= count32(matched[rule],clear,hit);
                breaches[rule] <= count32(breaches[rule],clear,hit && bad);
                rising[rule] <= count32(rising[rule],clear,hit && enter_alarm);
                falling[rule] <= count32(falling[rule],clear,hit && leave_alarm);
                losses[rule] <= count32(losses[rule],clear,hit && lost);
                if (clear || (hit && delta > worst[rule]))
                    worst[rule] <= hit ? delta : '0;
            end
        end
    end
endmodule

`default_nettype wire
