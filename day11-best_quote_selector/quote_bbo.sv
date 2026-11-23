`timescale 1ns/1ps
`include "quote_codes.svh"

// Freeze the candidates at a query handshake and rank the two sides separately.
// The snapshot and result registers form a two-entry elastic pipeline.
module quote_bbo #(
    parameter integer INSTRUMENTS = 4,
    parameter integer VENUES = 4,
    parameter integer PRICE_W = 20,
    parameter integer QTY_W = 14,
    parameter integer TIME_W = 16,
    parameter integer SEQ_W = 12,
    parameter integer TAG_W = 16,
    parameter integer INST_W = INSTRUMENTS > 1 ? $clog2(INSTRUMENTS + 1) : 1,
    parameter integer VENUE_W = VENUES > 1 ? $clog2(VENUES + 1) : 1,
    localparam integer PAYLOAD_W = SEQ_W + TIME_W + QTY_W,
    localparam integer RECORD_W = 2 * (PRICE_W + QTY_W) + TIME_W + SEQ_W
)(
    input wire                      clk, rst,
    input wire [TIME_W-1:0]         now_tick,
    input wire                      query_valid,
    output wire                     query_ready,
    input wire [INST_W-1:0]         query_instrument,
    input wire [TIME_W-1:0]         query_max_age,
    input wire [TAG_W-1:0]          query_tag,
    input wire [VENUES-1:0]         read_enabled, read_present,
    input wire [VENUES*RECORD_W-1:0] read_records,
    output logic                    result_valid,
    input wire                      result_ready,
    output logic [TAG_W-1:0]        result_tag,
    output logic [3:0]              result_code,
    output logic [2:0]              result_state,
    output logic [TIME_W-1:0]       result_tick,
    output logic [VENUES-1:0]       result_active, result_stale,
    output logic                    result_bid_valid, result_ask_valid,
    output logic [VENUE_W-1:0]      result_bid_venue, result_ask_venue,
    output logic [PRICE_W-1:0]      result_bid_price, result_ask_price,
    output logic [QTY_W-1:0]        result_bid_qty, result_ask_qty,
    output logic [TIME_W-1:0]       result_bid_age, result_ask_age,
    output logic [SEQ_W-1:0]        result_bid_sequence, result_ask_sequence,
    output logic signed [PRICE_W:0] result_spread
);
    logic snapshot_valid;
    logic [TAG_W-1:0] snapshot_tag;
    logic [3:0] snapshot_code;
    logic [TIME_W-1:0] snapshot_tick;
    logic [VENUES-1:0] snapshot_active, snapshot_stale;
    logic [VENUES-1:0] bid_eligible, ask_eligible;
    logic [VENUES*PRICE_W-1:0] bid_prices, ask_prices;
    logic [VENUES*PAYLOAD_W-1:0] bid_payloads, ask_payloads;
    wire result_space = !result_valid || result_ready;
    wire snapshot_space = !snapshot_valid || result_space;
    wire [3:0] decoded = int'(query_instrument) >= INSTRUMENTS ? `QUOTE_BAD_INST :
        query_max_age[TIME_W-1] ? `QUOTE_BAD_AGE : `QUOTE_OK;
    assign query_ready = !rst && snapshot_space;

    for (genvar v = 0; v < VENUES; v++) begin : g_candidates
        wire [SEQ_W-1:0] number;
        wire [TIME_W-1:0] last_update;
        wire [QTY_W-1:0] bid_qty, ask_qty;
        wire [PRICE_W-1:0] bid_price, ask_price;
        wire [TIME_W-1:0] age = now_tick - last_update;
        wire live = decoded == `QUOTE_OK && read_present[v] && read_enabled[v];
        wire stale = live && (age[TIME_W-1] || age > query_max_age);

        assign {number, last_update, bid_qty, ask_qty, bid_price, ask_price} =
            read_records[v*RECORD_W +: RECORD_W];

        // A later tick or update cannot change a snapshot stalled in this stage.
        always_ff @(posedge clk) begin
            if (query_ready && query_valid) begin
                snapshot_active[v] <= live && !stale;
                snapshot_stale[v] <= stale;
                bid_eligible[v] <= live && !stale && bid_qty != 0;
                ask_eligible[v] <= live && !stale && ask_qty != 0;
                bid_prices[v*PRICE_W +: PRICE_W] <= bid_price;
                ask_prices[v*PRICE_W +: PRICE_W] <= ask_price;
                bid_payloads[v*PAYLOAD_W +: PAYLOAD_W] <= {number, age, bid_qty};
                ask_payloads[v*PAYLOAD_W +: PAYLOAD_W] <= {number, age, ask_qty};
            end
        end
    end

    wire bid_valid, ask_valid;
    wire [PRICE_W-1:0] bid_price, ask_price;
    wire [VENUE_W-1:0] bid_venue, ask_venue;
    wire [PAYLOAD_W-1:0] bid_payload, ask_payload;
    quote_reduce_side #(.VENUES(VENUES), .PRICE_W(PRICE_W), .PAYLOAD_W(PAYLOAD_W),
                        .VENUE_W(VENUE_W), .HIGH_WINS(1'b1)) bid_tree (
        .candidate_valid(bid_eligible), .candidate_price(bid_prices),
        .candidate_payload(bid_payloads), .winner_valid(bid_valid),
        .winner_price(bid_price), .winner_payload(bid_payload), .winner_venue(bid_venue)
    );
    quote_reduce_side #(.VENUES(VENUES), .PRICE_W(PRICE_W), .PAYLOAD_W(PAYLOAD_W),
                        .VENUE_W(VENUE_W), .HIGH_WINS(1'b0)) ask_tree (
        .candidate_valid(ask_eligible), .candidate_price(ask_prices),
        .candidate_payload(ask_payloads), .winner_valid(ask_valid),
        .winner_price(ask_price), .winner_payload(ask_payload), .winner_venue(ask_venue)
    );

    // Classify the consolidated prices. A cross can arise across venue rows
    // even though each individual row rejects crossed updates.
    function automatic logic [2:0] classify(
        input logic have_bid,
        input logic have_ask,
        input logic [PRICE_W-1:0] bid,
        input logic [PRICE_W-1:0] ask
    );
        case ({have_bid, have_ask})
            2'b00: classify = `BOOK_EMPTY;
            2'b10: classify = `BOOK_BID_ONLY;
            2'b01: classify = `BOOK_ASK_ONLY;
            default: classify = bid < ask ? `BOOK_OPEN :
                bid == ask ? `BOOK_LOCKED : `BOOK_CROSSED;
        endcase
    endfunction

    // Control advances only when the next register can receive. Data behind an
    // unset valid bit is unspecified until the first successful transfer.
    always_ff @(posedge clk) begin
        if (rst) begin
            snapshot_valid <= 1'b0;
            result_valid <= 1'b0;
        end else begin
            if (query_ready) begin
                snapshot_valid <= query_valid;
                if (query_valid) begin
                    snapshot_tag <= query_tag;
                    snapshot_tick <= now_tick;
                    snapshot_code <= decoded;
                end
            end
            if (result_space) begin
                result_valid <= snapshot_valid;
                if (snapshot_valid) begin
                    result_tag <= snapshot_tag;
                    result_code <= snapshot_code;
                    result_tick <= snapshot_tick;
                    result_active <= snapshot_active;
                    result_stale <= snapshot_stale;
                    result_bid_valid <= bid_valid;
                    result_ask_valid <= ask_valid;
                    result_bid_venue <= bid_venue;
                    result_ask_venue <= ask_venue;
                    result_bid_price <= bid_price;
                    result_ask_price <= ask_price;
                    {result_bid_sequence, result_bid_age, result_bid_qty} <= bid_payload;
                    {result_ask_sequence, result_ask_age, result_ask_qty} <= ask_payload;
                    result_state <= classify(bid_valid, ask_valid, bid_price, ask_price);
                    result_spread <= bid_valid && ask_valid ?
                        $signed({1'b0, ask_price}) - $signed({1'b0, bid_price}) : '0;
                end
            end
        end
    end
endmodule
