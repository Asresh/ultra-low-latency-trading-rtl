`timescale 1ns/1ps
`default_nettype none

// Settle a normalized execution against a venue's marginal turnover tiers.
// Rates are signed basis points. Divide and round the total exactly once.
module tiered_fee_settlement #(
    parameter integer VENUES=3,
    parameter integer TIERS=4,
    parameter integer PRICE_FRAC=8,
    parameter integer REV_W=16,
    parameter integer SEQ_W=32,
    localparam integer VENUE_W=$clog2(VENUES+1),
    localparam integer BREAKS=TIERS>1 ? TIERS-1 : 1,
    localparam integer TIER_W=$clog2(TIERS+1)
) (
    input wire clk,
    input wire rst,

    input wire c_valid,
    output wire c_ready,
    input wire [1:0] c_op,
    input wire [VENUE_W-1:0] c_venue,
    input wire [REV_W-1:0] c_expected_revision,
    input wire [SEQ_W-1:0] c_next_sequence,
    input wire [63:0] c_turnover,
    input wire [64*BREAKS-1:0] c_thresholds,
    input wire [16*TIERS-1:0] c_maker_rates,
    input wire [16*TIERS-1:0] c_taker_rates,
    output wire c_rsp_valid,
    input wire c_rsp_ready,
    output logic [3:0] c_rsp_status,
    output logic [VENUE_W-1:0] c_rsp_venue,
    output logic [REV_W-1:0] c_rsp_revision,
    output logic [SEQ_W-1:0] c_rsp_sequence,
    output logic [63:0] c_rsp_turnover,
    output logic c_rsp_armed,

    input wire s_valid,
    output wire s_ready,
    input wire [VENUE_W-1:0] s_venue,
    input wire [15:0] s_tag,
    input wire [REV_W-1:0] s_revision,
    input wire [SEQ_W-1:0] s_sequence,
    input wire [31:0] s_price,
    input wire [31:0] s_quantity,
    input wire s_maker,
    input wire s_sell,
    input wire [1:0] s_round,

    output wire m_valid,
    input wire m_ready,
    output logic [VENUE_W-1:0] m_venue,
    output logic [15:0] m_tag,
    output logic [3:0] m_status,
    output logic [REV_W-1:0] m_revision,
    output logic [SEQ_W-1:0] m_sequence,
    output logic [63:0] m_gross,
    output logic signed [64:0] m_fee,
    output logic [64:0] m_cash,
    output logic [63:0] m_turnover,
    output logic [TIER_W-1:0] m_tiers,
    output logic m_inexact
);
    localparam logic [3:0] OK=0, BAD_VENUE=1, STALE_REVISION=2, BAD_PROFILE=3, REV_FULL=4,
                           CLOSED=5, OLD=6, GAP=7, BAD_FILL=8, TURNOVER_FULL=9, BAD_OP=10;
    localparam logic [1:0] INSTALL=0, INSPECT=1, FENCE=2;
    localparam logic [2:0] IDLE=0, GROSS=1, SLICES=2, FEE_START=3, FEE_WAIT=4, REPLY=5;
    localparam integer IDX_W=VENUES>1 ? $clog2(VENUES) : 1;
    localparam integer NUM_W=82;
    localparam logic [63:0] PRICE_SCALE=64'd1 << PRICE_FRAC;

    logic [2:0] state;
    logic [VENUES-1:0] armed;
    logic [REV_W-1:0] revision [0:VENUES-1];
    logic [SEQ_W-1:0] sequence_next [0:VENUES-1];
    logic [63:0] turnover [0:VENUES-1];
    logic [64*BREAKS-1:0] thresholds [0:VENUES-1];
    logic [16*TIERS-1:0] maker_rates [0:VENUES-1], taker_rates [0:VENUES-1];

    wire c_legal=int'(c_venue)<VENUES;
    wire s_legal=int'(s_venue)<VENUES;
    wire [IDX_W-1:0] control_idx=c_legal ? IDX_W'(c_venue) : '0;
    wire [IDX_W-1:0] input_idx=s_legal ? IDX_W'(s_venue) : '0;
    logic control_pending, profile_legal;
    logic [3:0] command_status,input_status;
    assign c_rsp_valid=!rst && control_pending;
    assign c_ready=!rst && state==IDLE && (!control_pending || c_rsp_ready);
    wire control_accept=c_valid && c_ready;
    assign s_ready=!rst && state==IDLE && !control_accept;
    assign m_valid=!rst && state==REPLY;
    wire configure=control_accept && command_status==OK && c_op==INSTALL;
    wire freeze=control_accept && command_status==OK && c_op==FENCE;

    function automatic logic rates_valid(input logic signed [15:0] rate);
        rates_valid=(rate>=-16'sd10000 && rate<=16'sd10000);
    endfunction

    always @* begin : check_profile
        logic [63:0] prior, stop;
        prior=0;
        stop=0;
        profile_legal=1;
        for(integer t=0;t<TIERS;t=t+1) begin
            if (t<TIERS-1) begin
                stop=c_thresholds[t*64+:64];
                if(stop<=prior || (&stop))
                    profile_legal=0;
                prior=stop;
            end
            if (!rates_valid($signed(c_maker_rates[16*t+:16])) ||
                !rates_valid($signed(c_taker_rates[16*t+:16])))
                profile_legal=0;
        end
    end

    always @* begin
        command_status=OK;
        if (!c_legal)
            command_status=BAD_VENUE;
        else if (c_op>FENCE)
            command_status=BAD_OP;
        else if (c_op!=INSPECT && c_expected_revision!=revision[control_idx])
            command_status=STALE_REVISION;
        else if (c_op==INSTALL) begin
            if (&revision[control_idx])
                command_status=REV_FULL;
            else if (!profile_legal)
                command_status=BAD_PROFILE;
        end

        input_status=OK;
        if (!s_legal)
            input_status=BAD_VENUE;
        else if (s_revision!=revision[input_idx])
            input_status=STALE_REVISION;
        else if (!armed[input_idx])
            input_status=CLOSED;
        else if (s_sequence<sequence_next[input_idx])
            input_status=OLD;
        else if (s_sequence>sequence_next[input_idx])
            input_status=GAP;
        else if (s_price==0 || s_quantity==0 || s_round==3)
            input_status=BAD_FILL;
    end

    logic [IDX_W-1:0] active_idx;
    logic [1:0] rounding;
    logic maker,sell;
    logic [63:0] product, begin_turnover,end_turnover;
    logic [TIER_W-1:0] tier_index;
    logic signed [NUM_W-1:0] numerator;
    wire [63:0] gross_q=product >> PRICE_FRAC;
    wire [63:0] gross_r=product & (PRICE_SCALE-1'b1);
    wire [63:0] gross_next=gross_q+64'(round_up(rounding,gross_q[0],gross_r,PRICE_SCALE));
    wire [64:0] total_turnover={1'b0,begin_turnover}+{1'b0,gross_next};
    logic [63:0] lower,upper;
    logic signed [15:0] rate;

    function automatic logic round_up(input logic [1:0] mode,
        input logic odd,input logic [63:0] remainder,denominator);
        logic [64:0] twice;
        twice={remainder,1'b0};
        round_up=(mode==1 && remainder!=0) ||
                 (mode==2 && (twice>{1'b0,denominator} ||
                               (twice=={1'b0,denominator} && odd)));
    endfunction

    always @* begin
        lower=begin_turnover;
        upper=end_turnover;
        if (tier_index!=0 && thresholds[active_idx][64*(int'(tier_index)-1)+:64]>lower)
            lower=thresholds[active_idx][64*(int'(tier_index)-1)+:64];
        if (tier_index<TIER_W'(TIERS-1) && thresholds[active_idx][64*int'(tier_index)+:64]<upper)
            upper=thresholds[active_idx][64*int'(tier_index)+:64];
        rate=maker ? $signed(maker_rates[active_idx][16*int'(tier_index)+:16]) :
                     $signed(taker_rates[active_idx][16*int'(tier_index)+:16]);
    end

    wire [63:0] slice=upper>lower ? upper-lower : 64'd0;
    wire signed [NUM_W-1:0] contribution=$signed(NUM_W'(slice))*NUM_W'(rate);
    wire [NUM_W-1:0] magnitude=numerator<0 ? $unsigned(-numerator) : $unsigned(numerator);
    wire divider_ready, divider_valid;
    wire [NUM_W-1:0] quotient;
    wire [NUM_W-65:0] unused_quotient_high=quotient[NUM_W-1:64];
    wire [13:0] remainder;
    wire [63:0] fee_magnitude=64'(quotient)+64'(round_up(rounding,quotient[0],64'(remainder),64'd10000));
    wire signed [64:0] fee_signed=numerator<0 ? -$signed({1'b0,fee_magnitude}) :
                                                             $signed({1'b0,fee_magnitude});
    wire signed [65:0] total_cash=$signed(66'(m_gross)) +
                                           (sell ? -66'(fee_signed) : 66'(fee_signed));

    notional_divide #(.NUM_W(NUM_W),.DIVISOR(10000)) divide_fee (
        .clk,.rst,.s_valid(state==FEE_START),.s_ready(divider_ready),.s_value(magnitude),
        .m_valid(divider_valid),.m_ready(state==FEE_WAIT),.m_quotient(quotient),.m_remainder(remainder)
    );

    always_ff @(posedge clk) begin
        if (rst) begin
            state<=IDLE;
            armed<='0;
            control_pending<=0;
            {c_rsp_status,c_rsp_venue,c_rsp_revision,c_rsp_sequence,c_rsp_turnover,c_rsp_armed}<='0;
            {m_venue,m_tag,m_status,m_revision,m_sequence,m_gross,m_fee,m_cash,
             m_turnover,m_tiers,m_inexact}<='0;
            {active_idx,rounding,maker,sell,product,begin_turnover,end_turnover,tier_index,numerator}<='0;
            for(integer venue=0;venue<VENUES;venue=venue+1)
                {revision[venue],sequence_next[venue],turnover[venue],thresholds[venue],
                 maker_rates[venue],taker_rates[venue]}<='0;
        end else begin
            if (c_rsp_ready)
                control_pending<=0;
            if (control_accept) begin
                control_pending<=1;
                {c_rsp_status,c_rsp_venue}<={command_status,c_venue};
                c_rsp_revision<=c_legal ? revision[control_idx]+REV_W'(configure) : '0;
                c_rsp_sequence<=!c_legal ? '0 :
                    configure ? c_next_sequence : sequence_next[control_idx];
                c_rsp_turnover<=!c_legal ? '0 : configure ? c_turnover : turnover[control_idx];
                c_rsp_armed<=c_legal && (configure || (!freeze && armed[control_idx]));
                if (configure) begin
                    armed[control_idx]<=1;
                    revision[control_idx]<=revision[control_idx]+1'b1;
                    sequence_next[control_idx]<=c_next_sequence;
                    turnover[control_idx]<=c_turnover;
                    thresholds[control_idx]<=c_thresholds;
                    maker_rates[control_idx]<=c_maker_rates;
                    taker_rates[control_idx]<=c_taker_rates;
                end
                if (freeze)
                    armed[control_idx]<=0;
            end

            case(state)
                IDLE: if (s_valid && s_ready) begin
                    {m_venue,m_tag,m_status,m_revision,m_sequence}<=
                        {s_venue,s_tag,input_status,s_revision,s_sequence};
                    {rounding,maker,sell}<={s_round,s_maker,s_sell};
                    m_turnover<=s_legal ? turnover[input_idx] : '0;
                    begin_turnover<=s_legal ? turnover[input_idx] : '0;
                    {m_gross,m_fee,m_cash,m_tiers,m_inexact}<='0;
                    product<=s_price*s_quantity;
                    active_idx<=input_idx;
                    state<=input_status==OK ? GROSS : REPLY;
                end
                GROSS: begin
                    if (total_turnover[64]) begin
                        m_status<=TURNOVER_FULL;
                        state<=REPLY;
                    end else begin
                        m_gross<=gross_next;
                        m_inexact<=gross_r!=0;
                        end_turnover<=total_turnover[63:0];
                        tier_index<='0;
                        numerator<='0;
                        state<=SLICES;
                    end
                end
                SLICES: begin
                    numerator<=numerator+contribution;
                    if (slice!=0)
                        m_tiers<=m_tiers+1'b1;
                    if (tier_index==TIER_W'(TIERS-1))
                        state<=FEE_START;
                    else
                        tier_index<=tier_index+1'b1;
                end
                FEE_START: if (divider_ready)
                    state<=FEE_WAIT;
                FEE_WAIT: if (divider_valid) begin
                    m_fee<=fee_signed;
                    m_cash<=total_cash<0 ? 65'd0 : 65'(total_cash);
                    m_turnover<=end_turnover;
                    m_inexact<=m_inexact || remainder!=0;
                    turnover[active_idx]<=end_turnover;
                    if (&m_sequence)
                        armed[active_idx]<=0;
                    else
                        sequence_next[active_idx]<=m_sequence+1'b1;
                    state<=REPLY;
                end
                REPLY: if (m_ready)
                    state<=IDLE;
                default: state<=IDLE;
            endcase
        end
    end
endmodule

`default_nettype wire
