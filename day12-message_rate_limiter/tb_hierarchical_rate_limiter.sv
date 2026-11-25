`timescale 1ns/1ps
`default_nettype none

module tb_hierarchical_rate_limiter #(
    parameter integer GROUPS = 3,
    parameter integer SESSIONS = 7,
    parameter integer TICK_W = 12,
    parameter integer TOKEN_W = 12,
    parameter integer COUNT_W = 16,
    parameter integer TAG_W = 16
);
    localparam integer GROUP_W = $clog2(GROUPS+1);
    localparam integer SESSION_W = $clog2(SESSIONS+1);
    localparam integer SNAPSHOT_W = 1 + TICK_W + 5*TOKEN_W + 2*COUNT_W + 2;
    localparam integer COMMAND_W = 5 + SESSION_W + GROUP_W + TICK_W + 3*TOKEN_W + TAG_W;
    localparam integer RESPONSE_W = 14 + SESSION_W + GROUP_W + TAG_W + 3*SNAPSHOT_W;

    logic clk = 0;
    always #5 clk = ~clk;
    logic rst = 1;
    logic s_valid = 0, m_ready = 0;
    wire s_ready, m_valid, m_critical;
    logic [COMMAND_W-1:0] command_word = 0;
    wire [2:0] s_op, m_op, m_scope, m_detail;
    wire [SESSION_W-1:0] s_session, m_session;
    wire [GROUP_W-1:0] s_group, m_group;
    wire [TICK_W-1:0] s_tick;
    wire [TOKEN_W-1:0] s_amount, s_rate, s_reserve;
    wire s_enable, s_critical;
    wire [TAG_W-1:0] s_tag, m_tag;
    wire [3:0] m_status;
    wire [SNAPSHOT_W-1:0] m_port, m_group_state, m_session_state;

    wire [TOKEN_W-1:0] port_balance = m_port[2*COUNT_W+2+:TOKEN_W];
    wire [TOKEN_W-1:0] group_balance = m_group_state[2*COUNT_W+2+:TOKEN_W];
    wire [TOKEN_W-1:0] session_balance = m_session_state[2*COUNT_W+2+:TOKEN_W];

    assign {s_op, s_session, s_group, s_tick, s_amount, s_rate, s_reserve, s_enable, s_critical, s_tag} = command_word;
    wire [RESPONSE_W-1:0] response_word = {
        m_op, m_session, m_group, m_tag, m_status, m_scope, m_detail, m_critical,
        m_port, m_group_state, m_session_state
    };

    hierarchical_rate_limiter #(
        .GROUPS(GROUPS), .SESSIONS(SESSIONS), .TICK_W(TICK_W), .TOKEN_W(TOKEN_W),
        .COUNT_W(COUNT_W), .TAG_W(TAG_W)
    ) dut (.*);

    initial begin : simulate
        string tape_path, wave_path;
        integer tape, matched;
        integer cycle = 0, accepted = 0, transferred = 0, stalled = 0;
        logic expected_ready, expected_valid;
        logic [RESPONSE_W-1:0] expected_result;
        if (!$value$plusargs("tape=%s", tape_path))
            $fatal(1,"Use +tape to supply the checked hierarchy scenario");
        tape = $fopen(tape_path, "r");
        if (!tape) $fatal(1,"Could not open %s", tape_path);
        if ($value$plusargs("wave=%s", wave_path)) begin
            $dumpfile(wave_path);
            $dumpvars(1, tb_hierarchical_rate_limiter);
        end
        matched = $fscanf(tape,"%b %b %b %h %b %b %h\n",
            rst, m_ready, s_valid, command_word, expected_ready, expected_valid, expected_result);
        while (matched == 7) begin
            @(posedge clk);
            if ({s_ready,m_valid} !== {expected_ready,expected_valid})
                $fatal(1,"Cycle %0d handshake %b/%b expected %b/%b", cycle,
                       s_ready, m_valid, expected_ready, expected_valid);
            if (m_valid && response_word !== expected_result)
                $fatal(1,"Cycle %0d response %h expected %h",cycle,response_word,expected_result);
            if (s_valid && s_ready) accepted++;
            if (m_valid && m_ready) transferred++;
            if (m_valid && !m_ready) stalled++;
            cycle++;
            @(negedge clk);
            matched = $fscanf(tape,"%b %b %b %h %b %b %h\n",
                rst, m_ready, s_valid, command_word, expected_ready, expected_valid, expected_result);
        end
        if (!$feof(tape) || !cycle || m_valid)
            $fatal(1,"Tape ended malformed or with a pending result at cycle %0d",cycle);
        $fclose(tape);
        $display("PASS hierarchy: %0d cycles %0d accepted %0d transferred %0d stalled",
                 cycle, accepted, transferred, stalled);
        $finish;
    end
endmodule

`default_nettype wire
