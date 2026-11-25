`timescale 1ns/1ps
`default_nettype none

module tb_deferred_rate_scheduler #(
    parameter integer GROUPS = 3, SESSIONS = 7, DEPTH = 4,
                      TOKEN_W = 12, TTL_W = 12, TAG_W = 16, DATA_W = 32
);
    localparam integer GROUP_W = $clog2(GROUPS+1), SESSION_W = $clog2(SESSIONS+1);
    localparam integer COUNT_W = $clog2(DEPTH+1);
    localparam integer ADMIN_W = 5+GROUP_W+SESSION_W+4*TOKEN_W+TAG_W;
    localparam integer OFFER_W = SESSION_W+TOKEN_W+TTL_W+TAG_W+DATA_W;
    localparam integer RESPONSE_W = 11+SESSION_W+GROUP_W+TAG_W+DATA_W+5*TOKEN_W+TTL_W+COUNT_W;
    logic clk = 0;
    always #5 clk = ~clk;
    logic rst = 1, m_ready = 0, a_valid = 0, s_valid = 0;
    logic [ADMIN_W-1:0] admin_word;
    logic [OFFER_W-1:0] offer_word;
    wire a_ready,s_ready,s_rejected,m_valid,m_admin,a_enable,a_critical;
    wire [2:0] a_op,m_op,m_detail;
    wire [3:0] m_status;
    wire [SESSION_W-1:0] a_session,s_session,m_session;
    wire [GROUP_W-1:0] a_group,m_group;
    wire [TAG_W-1:0] a_tag,s_tag,m_tag;
    wire [DATA_W-1:0] s_data,m_data;
    wire [TOKEN_W-1:0] a_capacity,a_rate,a_reserve,a_quantum,s_cost,m_cost,m_deficit;
    wire [TTL_W-1:0] s_ttl,m_ttl;
    wire [COUNT_W-1:0] m_depth;
    wire [3*TOKEN_W-1:0] m_balances;
    wire [SESSIONS*COUNT_W-1:0] occupancy;
    assign {a_op,a_session,a_group,a_capacity,a_rate,a_reserve,a_quantum,a_enable,a_critical,a_tag} = admin_word;
    assign {s_session,s_cost,s_ttl,s_tag,s_data} = offer_word;
    wire [RESPONSE_W-1:0] response_word = {
        m_admin,m_op,m_status,m_detail,m_session,m_group,m_tag,m_data,m_cost,m_ttl,m_deficit,m_depth,m_balances
    };

    deferred_rate_scheduler #(
        .GROUPS(GROUPS),.SESSIONS(SESSIONS),.DEPTH(DEPTH),.TOKEN_W(TOKEN_W),
        .TTL_W(TTL_W),.TAG_W(TAG_W),.DATA_W(DATA_W)
    ) dut (.*);

    initial begin : test
        integer tape, count, cycle = 0, offered = 0, accepted = 0, retired = 0, stalled = 0;
        logic expected_admin, expected_source, expected_reject, expected_valid;
        logic [RESPONSE_W-1:0] expected_word;
        logic [SESSIONS*COUNT_W-1:0] expected_occupancy;
        string source, wave;
        if (!$value$plusargs("tape=%s",source)) $fatal(1,"supply +tape=path");
        tape = $fopen(source,"r");
        if (!tape) $fatal(1,"cannot open stimulus");
        if ($value$plusargs("wave=%s",wave)) begin
            $dumpfile(wave);
            $dumpvars(1,tb_deferred_rate_scheduler);
        end
        @(negedge clk);
        while (!$feof(tape)) begin
            count = $fscanf(tape,"%b %b %b %h %b %h %b %b %b %b %h %h\n",
                rst,m_ready,a_valid,admin_word,s_valid,offer_word,
                expected_admin,expected_source,expected_reject,expected_valid,expected_word,expected_occupancy);
            if (count != 12) $fatal(1,"short stimulus line %0d",cycle);
            @(posedge clk);
            if ({a_ready,s_ready,s_rejected,m_valid,occupancy} !==
                {expected_admin,expected_source,expected_reject,expected_valid,expected_occupancy})
                $fatal(1,"Cycle %0d pins %b %b %b %b %h expected %b %b %b %b %h",cycle,
                       a_ready,s_ready,s_rejected,m_valid,occupancy,expected_admin,expected_source,
                       expected_reject,expected_valid,expected_occupancy);
            if (m_valid && response_word !== expected_word)
                $fatal(1,"Cycle %0d reply %h expected %h",cycle,response_word,expected_word);
            if (s_valid) offered++;
            if (s_valid && s_ready && !s_rejected) accepted++;
            if (m_valid && m_ready && !m_admin) retired++;
            if (m_valid && !m_ready) stalled++;
            cycle++;
            @(negedge clk);
        end
        if (m_valid || (|occupancy)) $fatal(1,"scenario stopped before drain");
        $fclose(tape);
        $display("PASS deferred scheduler: %0d cycles, %0d/%0d ingress, %0d retired, %0d held result cycles",
            cycle,accepted,offered,retired,stalled);
        $finish;
    end
endmodule

`default_nettype wire
