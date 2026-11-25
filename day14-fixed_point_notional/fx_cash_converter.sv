`timescale 1ns/1ps
`default_nettype none

// Convert signed cash through a direct or explicit pivot rate. Each request
// observes an atomic rate-table snapshot and rounds only its final amount.
module fx_cash_converter #(
    parameter integer CURRENCIES=8,
    parameter integer ROWS=12,
    parameter integer RATE_W=32,
    parameter integer RATE_FRAC=24,
    parameter integer TIME_W=48,
    parameter integer EPOCH_W=16,
    localparam integer CCY_W=$clog2(CURRENCIES+1),
    localparam integer ROW_W=$clog2(ROWS+1)
) (
    input wire clk,
    input wire rst,
    input wire [TIME_W-1:0] tick,
    input wire cfg_valid,
    output wire cfg_ready,
    input wire [ROW_W-1:0] cfg_row,
    input wire cfg_enable,
    input wire [CCY_W-1:0] cfg_from,cfg_to,
    input wire [RATE_W-1:0] cfg_bid,cfg_ask,
    input wire [TIME_W-1:0] cfg_observed_tick,
    output logic cfg_error,
    input wire commit_valid,
    output wire commit_ready,
    output logic [EPOCH_W-1:0] active_epoch,

    input wire s_valid,
    output wire s_ready,
    input wire [15:0] s_tag,
    input wire [CCY_W-1:0] s_from,s_to,
    input wire s_allow_pivot,
    input wire [CCY_W-1:0] s_pivot,
    input wire [TIME_W-1:0] s_max_age,
    input wire signed [64:0] s_amount,
    input wire [1:0] s_round,

    output wire m_valid,
    input wire m_ready,
    output logic [15:0] m_tag,
    output logic [2:0] m_status,
    output logic signed [64:0] m_amount,
    output logic [1:0] m_hops,
    output logic [ROW_W-1:0] m_first_row,m_second_row,
    output logic [TIME_W-1:0] m_oldest_age,
    output logic [EPOCH_W-1:0] m_epoch,
    output logic m_inexact
);
    localparam logic [2:0] OK=0, BAD_REQUEST=1, MISS=2, STALE=3, OVERFLOW=4;
    localparam logic [1:0] IDLE=0, FIRST=1, SECOND=2, REPLY=3;
    localparam integer NUM_W=65+2*RATE_W;
    localparam logic [RATE_W-1:0] UNITY=RATE_W'(1)<<RATE_FRAC;
    localparam logic [NUM_W-1:0] SCALE=NUM_W'(1)<<(2*RATE_FRAC);
    localparam logic [NUM_W-1:0] LIMIT=NUM_W'(1)<<64;
    localparam integer INDEX_W=ROWS>1 ? $clog2(ROWS) : 1;

    logic bank;
    logic [ROWS-1:0] enabled [0:1];
    logic [CCY_W-1:0] from_ccy [0:1][0:ROWS-1], to_ccy [0:1][0:ROWS-1];
    logic [RATE_W-1:0] bid_rate [0:1][0:ROWS-1], ask_rate [0:1][0:ROWS-1];
    logic [TIME_W-1:0] observed [0:1][0:ROWS-1];
    logic [1:0] state;
    logic negative;
    logic [1:0] rounding;
    logic [RATE_W-1:0] first_rate,second_rate;
    logic [64:0] magnitude;
    logic [64+RATE_W:0] intermediate;

    logic [2:0] found, known;
    logic [INDEX_W-1:0] selected [0:2];
    logic [TIME_W-1:0] ages [0:2];
    wire config_ok=!cfg_enable || (int'(cfg_from)<CURRENCIES && int'(cfg_to)<CURRENCIES &&
                                  cfg_from!=cfg_to && cfg_bid!=0 && cfg_bid<=cfg_ask);
    wire [INDEX_W-1:0] config_index=INDEX_W'(cfg_row);
    wire pivot_legal=s_allow_pivot && int'(s_pivot)<CURRENCIES && s_pivot!=s_from && s_pivot!=s_to;
    wire input_ok=int'(s_from)<CURRENCIES && int'(s_to)<CURRENCIES && s_round!=3 &&
                  (!s_allow_pivot || s_from==s_to || pivot_legal);
    wire [64:0] input_abs=s_amount[64] ? $unsigned(-s_amount) : $unsigned(s_amount);
    wire direct_ok=found[0];
    wire indirect_ok=found[1] && found[2];
    wire use_pivot=!direct_ok && indirect_ok;

    assign commit_ready=!rst;
    assign cfg_ready=!rst && !commit_valid;
    assign s_ready=!rst && state==IDLE;
    assign m_valid=!rst && state==REPLY;

    always @* begin : select_quote_rows
        logic [2:0] match;
        logic [TIME_W-1:0] age;
        logic fresh;
        found='0;
        known='0;
        for(integer item=0;item<3;item=item+1) begin
            selected[item]='0;
            ages[item]='0;
        end
        for(integer row=0;row<ROWS;row=row+1) begin
            match[0]=from_ccy[bank][row]==s_from && to_ccy[bank][row]==s_to;
            match[1]=pivot_legal && from_ccy[bank][row]==s_from && to_ccy[bank][row]==s_pivot;
            match[2]=pivot_legal && from_ccy[bank][row]==s_pivot && to_ccy[bank][row]==s_to;
            age=tick-observed[bank][row];
            fresh=tick>=observed[bank][row] && age<=s_max_age;
            if(enabled[bank][row]) begin
                known=known | match;
                for(integer path=0;path<3;path=path+1) begin
                    if(match[path] && fresh && !found[path]) begin
                        found[path]=1;
                        selected[path]=INDEX_W'(row);
                        ages[path]=age;
                    end
                end
            end
        end
    end

    wire [INDEX_W-1:0] left=use_pivot ? selected[1] : selected[0];
    wire [INDEX_W-1:0] right=selected[2];
    wire [TIME_W-1:0] pivot_age=ages[1]>ages[2] ? ages[1] : ages[2];
    wire [NUM_W-1:0] result=intermediate*second_rate;
    wire [NUM_W-1:0] whole=result>>(2*RATE_FRAC);
    wire [NUM_W-1:0] remainder=result&(SCALE-1'b1);
    wire [NUM_W:0] twice={remainder,1'b0};
    wire round_up=rounding==1 ? remainder!=0 : rounding==2 &&
                  (twice>{1'b0,SCALE} || (twice=={1'b0,SCALE} && whole[0]));
    wire [NUM_W-1:0] rounded=whole+NUM_W'(round_up);
    wire out_of_range=negative ? rounded>LIMIT : rounded>=LIMIT;
    wire signed [64:0] signed_amount=negative ? -$signed(rounded[64:0]) : $signed(rounded[64:0]);

    always_ff @(posedge clk) begin
        if(rst) begin
            {enabled[0],enabled[1],bank,active_epoch,cfg_error} <= '0;
            state <= IDLE;
            {negative,rounding,first_rate,second_rate,magnitude,intermediate} <= '0;
            {m_tag,m_status,m_amount,m_hops,m_first_row,m_second_row,m_oldest_age,m_epoch,m_inexact} <= '0;
        end else begin
            cfg_error <= 0;
            if(commit_valid && commit_ready) begin
                bank <= !bank;
                active_epoch <= active_epoch+1'b1;
            end
            if(cfg_valid && cfg_ready) begin
                if(int'(cfg_row)>=ROWS || !config_ok) begin
                    cfg_error <= 1;
                end else begin
                    enabled[!bank][config_index] <= cfg_enable;
                    {from_ccy[!bank][config_index],to_ccy[!bank][config_index]} <= {cfg_from,cfg_to};
                    {bid_rate[!bank][config_index],ask_rate[!bank][config_index]} <= {cfg_bid,cfg_ask};
                    observed[!bank][config_index] <= cfg_observed_tick;
                end
            end

            case(state)
                IDLE: if(s_valid && s_ready) begin
                    m_tag <= s_tag;
                    m_epoch <= active_epoch;
                    {m_amount,m_hops,m_oldest_age,m_inexact} <= '0;
                    {m_first_row,m_second_row} <= '1;
                    m_status <= OK;
                    if(!input_ok) begin
                        m_status <= BAD_REQUEST;
                        state <= REPLY;
                    end else if(s_from==s_to) begin
                        m_amount <= s_amount;
                        state <= REPLY;
                    end else if(!direct_ok && !indirect_ok) begin
                        m_status <= known[0] || (&known[2:1]) ? STALE : MISS;
                        state <= REPLY;
                    end else begin
                        m_hops <= use_pivot ? 2'd2 : 2'd1;
                        m_first_row <= ROW_W'(left);
                        m_second_row <= use_pivot ? ROW_W'(right) : {ROW_W{1'b1}};
                        m_oldest_age <= use_pivot ? pivot_age : ages[0];
                        first_rate <= s_amount[64] ? bid_rate[bank][left] : ask_rate[bank][left];
                        second_rate <= !use_pivot ? UNITY :
                            s_amount[64] ? bid_rate[bank][right] : ask_rate[bank][right];
                        negative <= s_amount[64];
                        rounding <= s_round;
                        magnitude <= input_abs;
                        state <= FIRST;
                    end
                end
                FIRST: begin
                    intermediate <= magnitude*first_rate;
                    state <= SECOND;
                end
                SECOND: begin
                    m_amount <= out_of_range ? 65'sd0 : signed_amount;
                    m_status <= out_of_range ? OVERFLOW : OK;
                    m_inexact <= remainder!=0;
                    state <= REPLY;
                end
                REPLY: if(m_ready) state <= IDLE;
                default: state <= IDLE;
            endcase
        end
    end
`ifdef FORMAL_FX
    logic initialized=0;
    always_ff @(posedge clk) begin
        initialized<=1;
        if(!initialized) assume(rst);
        if(initialized && !rst && !$past(rst)) begin
            if($past(s_valid && s_ready))
                assert(m_epoch==$past(active_epoch));
            if($past(commit_valid && commit_ready))
                assert(active_epoch==$past(active_epoch)+1'b1);
            if($past(m_valid && !m_ready))
                assert(m_valid && $stable({m_tag,m_status,m_amount,m_hops,m_first_row,m_second_row,
                                           m_oldest_age,m_epoch,m_inexact}));
            if(m_valid && m_status==OVERFLOW)
                assert(m_amount==0 && m_hops!=0);
            if(m_valid && m_status==OK && m_hops==2)
                assert(m_first_row!=m_second_row);
            cover(s_valid && s_ready && commit_valid);
            cover(m_valid && m_status==OK && m_hops==2);
            cover(m_valid && m_status==OVERFLOW);
        end
    end
`endif

endmodule

`default_nettype wire
