`default_nettype none

module formal_publication(input wire clk);
    localparam integer BANKS = 3;
    localparam integer PW = 2, QW = 2, EW = 2, RVW = 2;
    localparam integer BW = $clog2(BANKS+1);
    localparam integer KW = 1+EW+2*PW+QW;
    localparam integer OUT_W = BW+KW+RVW+3;

    (* anyseq *) wire rst, q_ready;
    (* anyseq *) wire [BANKS*KW-1:0] desired_now;
    (* anyseq *) wire [BANKS*3-1:0] reason_now;
    wire q_valid, q_current, q_active;
    wire [BW-1:0] q_instrument;
    wire [EW-1:0] q_epoch;
    wire [RVW-1:0] q_revision;
    wire [PW-1:0] q_bid, q_ask;
    wire [QW-1:0] q_size;
    wire [2:0] q_reason;
    wire [KW-1:0] output_key = {q_active, q_epoch, q_bid, q_ask, q_size};
    wire [OUT_W-1:0] publication = {q_instrument, output_key, q_revision, q_reason};

    quote_publication_pipeline #(
        .BANKS(BANKS), .PRICE_W(PW), .QTY_W(QW), .EPOCH_W(EW), .REV_W(RVW)
    ) dut (.*);

    reg sampled = 1'b0;
    reg mature = 1'b0;
    reg [BANKS*KW-1:0] prior_keys, older_keys;
    reg [BANKS*3-1:0] prior_reasons, older_reasons;
    reg [RVW-1:0] next_revision [0:BANKS-1];
    reg [KW-1:0] prior_sent [0:BANKS-1];
    reg [BANKS-1:0] seen;
    reg [BANKS-1:0] invalidated;

    always @(posedge clk) begin
        sampled <= 1'b1;
        mature <= sampled;
        if (!sampled)
            assume(rst);
        prior_keys <= desired_now;
        older_keys <= prior_keys;
        prior_reasons <= reason_now;
        older_reasons <= prior_reasons;

        if (rst) begin
            seen <= '0;
            invalidated <= '0;
            for (integer bank = 0; bank < BANKS; bank++) begin
                next_revision[bank] <= '0;
                prior_sent[bank] <= '0;
            end
            assert(!q_valid && !q_current);
        end else begin
            for (integer bank = 0; bank < BANKS; bank++) begin
                if (seen[bank] && prior_sent[bank][KW-1] &&
                    desired_now[(bank+1)*KW-1-:EW+1] != prior_sent[bank][KW-1-:EW+1])
                    invalidated[bank] <= 1'b1;
            end
            if (sampled && !$past(rst) && $past(q_valid && !q_ready)) begin
                assert(q_valid);
                assert(publication == $past(publication));
            end
            if (!q_valid) begin
                assert(!q_current);
            end else begin
                assert(q_instrument < BW'(BANKS));
                for (integer bank = 0; bank < BANKS; bank++) begin
                    if (q_instrument == BW'(bank)) begin
                        assert(q_current == (output_key == desired_now[bank*KW+:KW]));
                        if (($past(rst) || !$past(q_valid) || $past(q_ready)) &&
                            mature && !$past(rst, 2)) begin
                            assert(output_key == older_keys[bank*KW+:KW]);
                            assert(q_reason == older_reasons[bank*3+:3]);
                        end
                        if (q_ready) begin
                            assert(q_revision == next_revision[bank]);
                            if (seen[bank]) begin
                                assert(q_active || prior_sent[bank][KW-1]);
                                if (output_key == prior_sent[bank])
                                    assert(q_active && invalidated[bank]);
                            end else begin
                                assert(q_active);
                            end
                            seen[bank] <= 1'b1;
                            prior_sent[bank] <= output_key;
                            invalidated[bank] <= q_active && !q_current;
                            next_revision[bank] <= next_revision[bank]+RVW'(1);
                        end
                    end
                end
            end
        end
        cover(mature && (&seen) && q_valid && q_ready);
        cover(mature && q_valid && !q_ready && !q_current && q_active);
        cover(mature && q_valid && q_ready && !q_active);
    end
endmodule

`default_nettype wire
