`timescale 1ns/1ps
`default_nettype none
`include "quote_inventory_codes.svh"

// A strategy inventory stream has one ordered sequence per instrument and
// explicit revisioned recovery. The last exact fill identity is retained.
module quote_inventory_ledger #(
    parameter integer INSTRUMENTS=3,
    parameter integer POS_W=24,
    parameter integer QTY_W=16,
    parameter integer PRICE_W=12,
    parameter integer SEQ_W=32,
    parameter integer REV_W=16,
    parameter integer TAG_W=16,
    localparam integer INST_W=$clog2(INSTRUMENTS+1),
    localparam integer POLICY_W=4*POS_W+2*QTY_W+PRICE_W
) (
    input wire clk,
    input wire rst,
    input wire c_valid,
    output wire c_ready,
    input wire [1:0] c_op,
    input wire [INST_W-1:0] c_instrument,
    input wire [TAG_W-1:0] c_tag,
    input wire [REV_W-1:0] c_expected_revision,
    input wire [SEQ_W-1:0] c_sequence,
    input wire c_sell,
    input wire [QTY_W-1:0] c_quantity,
    input wire signed [POS_W-1:0] c_position,
    input wire signed [POS_W-1:0] c_floor,
    input wire signed [POS_W-1:0] c_ceiling,
    input wire [QTY_W-1:0] c_bid_cap,
    input wire [QTY_W-1:0] c_ask_cap,
    input wire [POS_W-1:0] c_skew_trigger,
    input wire [PRICE_W-1:0] c_skew_ticks,

    output wire r_valid,
    input wire r_ready,
    output logic [1:0] r_op,
    output logic [INST_W-1:0] r_instrument,
    output logic [TAG_W-1:0] r_tag,
    output logic [3:0] r_status,
    output logic r_armed,
    output logic [REV_W-1:0] r_revision,
    output logic [SEQ_W-1:0] r_next_sequence,
    output logic signed [POS_W-1:0] r_position,
    output logic [31:0] r_fills,
    output logic [31:0] r_faults,

    output wire [INSTRUMENTS-1:0] armed_banks,
    output wire [INSTRUMENTS-1:0] changing_banks,
    output wire [INSTRUMENTS*REV_W-1:0] revisions,
    output wire [INSTRUMENTS*POLICY_W-1:0] policy
);
    localparam logic [1:0] INSTALL=0, FILL=1, INSPECT=2, FENCE=3;
    localparam integer INDEX_W=INSTRUMENTS > 1 ? $clog2(INSTRUMENTS) : 1;
    localparam integer ARITH_W=(POS_W > QTY_W ? POS_W : QTY_W)+2;
    logic [INSTRUMENTS-1:0] armed, seen_fill;
    logic [REV_W-1:0] revision [0:INSTRUMENTS-1];
    logic [SEQ_W-1:0] next_sequence [0:INSTRUMENTS-1], last_sequence [0:INSTRUMENTS-1];
    logic last_sell [0:INSTRUMENTS-1];
    logic [QTY_W-1:0] last_qty [0:INSTRUMENTS-1];
    logic signed [POS_W-1:0] position [0:INSTRUMENTS-1];
    logic signed [POS_W-1:0] floor_pos [0:INSTRUMENTS-1], ceiling_pos [0:INSTRUMENTS-1];
    logic [QTY_W-1:0] bid_cap [0:INSTRUMENTS-1], ask_cap [0:INSTRUMENTS-1];
    logic [POS_W-1:0] skew_trigger [0:INSTRUMENTS-1];
    logic [PRICE_W-1:0] skew_ticks [0:INSTRUMENTS-1];
    logic [31:0] fills [0:INSTRUMENTS-1], faults [0:INSTRUMENTS-1];
    logic pending;
    wire legal_instrument = int'(c_instrument) < INSTRUMENTS;
    wire [INDEX_W-1:0] at = legal_instrument ? INDEX_W'(c_instrument) : '0;
    wire same_revision = c_expected_revision == revision[at];
    wire signed [ARITH_W-1:0] delta = $signed(ARITH_W'(c_quantity));
    wire signed [ARITH_W-1:0] observed_position =
        ARITH_W'(position[at]) + (c_sell ? -delta : delta);
    wire fits_position = observed_position == ARITH_W'($signed(observed_position[POS_W-1:0]));
    logic [3:0] status;
    logic install, fill, fence, fault;
    wire take = c_valid && c_ready;
    assign c_ready = !rst && (!pending || r_ready);
    assign r_valid = !rst && pending;
    assign armed_banks = armed;

    function automatic logic [31:0] inc32(input logic [31:0] val);
        inc32 = (&val) ? val : val+1'b1;
    endfunction

    always @* begin
        status = `QI_OK;
        install = 0;
        fill = 0;
        fence = 0;
        fault = 0;
        if (!legal_instrument)
            status = `QI_BAD_INSTRUMENT;
        else if (c_op != INSPECT && !same_revision)
            status = `QI_STALE_EPOCH;
        else if (c_op == INSTALL) begin
            if (&revision[at])
                status = `QI_EPOCH_EXHAUSTED;
            else if (c_floor > 0 || c_ceiling < 0)
                status = `QI_BAD_POLICY;
            else
                install = 1;
        end else if (c_op == FENCE) begin
            fence = 1;
        end else if (c_op == FILL) begin
            if (seen_fill[at] && c_sequence == last_sequence[at]) begin
                if ({c_sell,c_quantity} == {last_sell[at],last_qty[at]})
                    status = `QI_DUPLICATE;
                else begin
                    status = `QI_CONFLICT;
                    fault = 1;
                end
            end else if (!armed[at]) begin
                status = `QI_FENCED;
            end else if (c_sequence < next_sequence[at]) begin
                status = `QI_OLD;
            end else if (c_sequence > next_sequence[at]) begin
                status = `QI_GAP;
                fault = 1;
            end else if (c_quantity == 0) begin
                status = `QI_ZERO_FILL;
                fault = 1;
            end else if (!fits_position) begin
                status = `QI_POSITION_OVERFLOW;
                fault = 1;
            end else begin
                fill = 1;
                fence = &c_sequence;
            end
        end
    end

    for (genvar bank=0;bank<INSTRUMENTS;bank=bank+1) begin : expose
        assign revisions[REV_W*bank+:REV_W] = revision[bank];
        assign policy[POLICY_W*bank+:POLICY_W] = {
            position[bank],floor_pos[bank],ceiling_pos[bank],bid_cap[bank],ask_cap[bank],
            skew_trigger[bank],skew_ticks[bank]
        };
        assign changing_banks[bank] = take && at == INDEX_W'(bank) && (install || fill || fence || fault);
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            armed <= '0;
            seen_fill <= '0;
            pending <= 0;
            {r_op,r_instrument,r_tag,r_status,r_armed,r_revision,r_next_sequence,
             r_position,r_fills,r_faults} <= '0;
            for (integer i=0; i<INSTRUMENTS; i=i+1) begin
                {revision[i],next_sequence[i],last_sequence[i],last_sell[i],last_qty[i],
                 position[i],floor_pos[i],ceiling_pos[i],bid_cap[i],ask_cap[i],
                 skew_trigger[i],skew_ticks[i],fills[i],faults[i]} <= '0;
            end
        end else if (c_ready) begin
            pending <= c_valid;
            if (take) begin
                {r_op,r_instrument,r_tag,r_status} <= {c_op,c_instrument,c_tag,status};
                r_armed <= legal_instrument && (install || (armed[at] && !fence && !fault));
                r_revision <= legal_instrument ? revision[at]+REV_W'(install) : '0;
                r_next_sequence <= !legal_instrument ? '0 : install ? c_sequence :
                    fill && !fence ? next_sequence[at]+1'b1 : next_sequence[at];
                r_position <= !legal_instrument ? '0 : install ? c_position :
                              fill ? POS_W'(observed_position) : position[at];
                r_fills <= !legal_instrument ? '0 : fill ? inc32(fills[at]) : fills[at];
                r_faults <= !legal_instrument ? '0 : fault ? inc32(faults[at]) : faults[at];
                if (install) begin
                    armed[at] <= 1;
                    seen_fill[at] <= 0;
                    revision[at] <= revision[at]+1'b1;
                    next_sequence[at] <= c_sequence;
                    position[at] <= c_position;
                    floor_pos[at] <= c_floor;
                    ceiling_pos[at] <= c_ceiling;
                    bid_cap[at] <= c_bid_cap;
                    ask_cap[at] <= c_ask_cap;
                    skew_trigger[at] <= c_skew_trigger;
                    skew_ticks[at] <= c_skew_ticks;
                end
                if (fill) begin
                    seen_fill[at] <= 1;
                    {last_sequence[at],last_sell[at],last_qty[at]} <= {c_sequence,c_sell,c_quantity};
                    position[at] <= POS_W'(observed_position);
                    fills[at] <= inc32(fills[at]);
                    if (!fence)
                        next_sequence[at] <= next_sequence[at]+1'b1;
                end
                if (fence || fault)
                    armed[at] <= 0;
                if (fault)
                    faults[at] <= inc32(faults[at]);
            end
        end
    end
`ifdef FORMAL_INVENTORY
    logic sampled = 0;
    always_ff @(posedge clk) begin
        sampled <= 1;
        if (!sampled)
            assume (rst);
        if (sampled && !rst && !$past(rst)) begin
            if ($past(r_valid && !r_ready))
                assert (r_valid && $stable({r_op,r_instrument,r_tag,r_status,r_armed,
                                           r_revision,r_next_sequence,r_position,r_fills,r_faults}));
            if (take && fill)
                assert (fits_position && armed[at] && same_revision && c_quantity != 0);
            if (take && install)
                assert (!(&revision[at]) && c_floor <= 0 && c_ceiling >= 0);
            cover (take && fill && fence);
            cover (take && fault && status == `QI_POSITION_OVERFLOW);
            cover (take && status == `QI_DUPLICATE);
        end
    end
`endif

endmodule

`default_nettype wire
