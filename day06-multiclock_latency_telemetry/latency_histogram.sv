`timescale 1ns/1ps
`default_nettype none

// LTS1 snapshot serializer. A snapshot swaps banks without stopping samples.
// The closed bank is streamed under ready/valid, then scrubbed incrementally.
module latency_histogram #(
    parameter integer CLASSES = 4,
    parameter integer BINS = 24,
    parameter integer COUNTER_W = 32,
    localparam integer CLASS_W = CLASSES > 1 ? $clog2(CLASSES) : 1,
    localparam integer BIN_W = $clog2(BINS)
) (
    input  wire               clk,
    input  wire               reset,
    input  wire               sample_valid,
    input  wire [CLASS_W-1:0] sample_class,
    input  wire [63:0]        sample_latency,
    input  wire               snapshot_valid,
    output wire               snapshot_ready,
    input  wire [63:0]        snapshot_tick,
    output logic              m_valid,
    input  wire               m_ready,
    output logic [127:0]      m_word,
    output wire               m_first,
    output wire               m_last,
    output logic [31:0]       completed,
    output wire               busy
);
    localparam logic [2:0] IDLE = 0, HEADER = 1, CLASS_TOTAL = 2,
                           CLASS_BOUNDS = 3, BUCKET = 4, CLEARING = 5;

    logic [COUNTER_W-1:0] bin_counts [0:1][0:CLASSES-1][0:BINS-1];
    logic [COUNTER_W-1:0] counts [0:1][0:CLASSES-1];
    logic [63:0] sums [0:1][0:CLASSES-1];
    logic [63:0] minimums [0:1][0:CLASSES-1];
    logic [63:0] maximums [0:1][0:CLASSES-1];
    logic overflow [0:1][0:CLASSES-1];
    logic active_bank;
    logic [2:0] state;
    logic [CLASS_W-1:0] class_no;
    logic [BIN_W-1:0] bin_no;
    logic [63:0] cut_tick;
    logic [31:0] frame_number;

    // Find the logarithmic bucket corresponding to a raw-tick duration.
    function automatic logic [BIN_W-1:0] latency_bin(input logic [63:0] delay);
        integer index;
        begin
            index = int'(delay[1:0]);
            for (integer bit_no = 2; bit_no < 64; bit_no = bit_no + 1)
                if (delay[bit_no])
                    index = bit_no + 2;
            latency_bin = index >= BINS ? BIN_W'(BINS-1) : BIN_W'(index);
        end
    endfunction

    wire closed_bank = !active_bank;
    wire [BIN_W-1:0] classified = latency_bin(sample_latency);
    wire [64:0] new_sum = {1'b0, sums[active_bank][sample_class]} + {1'b0, sample_latency};
    wire valid_class = {1'b0, sample_class} < (CLASS_W+1)'(CLASSES);
    wire end_class = class_no == CLASS_W'(CLASSES - 1);
    wire end_bin = bin_no == BIN_W'(BINS - 1);

    assign snapshot_ready = !reset && state == IDLE;
    assign busy = state != IDLE;
    assign m_first = m_valid && state == HEADER;
    assign m_last = m_valid && state == BUCKET && end_class && end_bin;

    always @* begin
        m_word = '0;
        m_valid = !reset && state != IDLE && state != CLEARING;
        case (state)
            HEADER: begin
                m_word[127:96] = 32'h4c54_5331;
                m_word[95:64] = frame_number;
                m_word[63:0] = cut_tick;
            end
            CLASS_TOTAL: begin
                m_word[127:124] = 4'd1;
                m_word[123:116] = 8'(class_no);
                m_word[115:108] = 8'(BINS);
                m_word[107:100] = 8'(COUNTER_W);
                m_word[99] = overflow[closed_bank][class_no];
                m_word[95:64] = 32'(counts[closed_bank][class_no]);
                m_word[63:0] = sums[closed_bank][class_no];
            end
            CLASS_BOUNDS: begin
                // The raw maximum is sent in bucket zero. This record holds
                // the full minimum and the high 52 bits of the maximum.
                m_word[127:124] = 4'd2;
                m_word[123:116] = 8'(class_no);
                m_word[115:64] = maximums[closed_bank][class_no][63:12];
                m_word[63:0] = counts[closed_bank][class_no] == 0 ?
                    64'd0 : minimums[closed_bank][class_no];
            end
            BUCKET: begin
                m_word[127:124] = 4'd3;
                m_word[123:116] = 8'(class_no);
                m_word[115:108] = 8'(bin_no);
                if (bin_no == '0)
                    m_word[107:96] = maximums[closed_bank][class_no][11:0];
                m_word[31:0] = 32'(bin_counts[closed_bank][class_no][bin_no]);
            end
            default: ;
        endcase
    end

    always_ff @(posedge clk) begin : update_banks
        if (reset) begin
            active_bank <= 1'b0;
            state <= IDLE;
            class_no <= '0;
            bin_no <= '0;
            cut_tick <= '0;
            frame_number <= '0;
            completed <= '0;
            for (integer bank = 0; bank < 2; bank = bank + 1) begin
                for (integer group = 0; group < CLASSES; group = group + 1) begin
                    counts[bank][group] <= '0;
                    sums[bank][group] <= '0;
                    minimums[bank][group] <= '1;
                    maximums[bank][group] <= '0;
                    overflow[bank][group] <= 1'b0;
                    for (integer bin = 0; bin < BINS; bin = bin + 1)
                        bin_counts[bank][group][bin] <= '0;
                end
            end
        end else begin
            if (sample_valid && valid_class) begin
                if (bin_counts[active_bank][sample_class][classified] != '1)
                    bin_counts[active_bank][sample_class][classified] <=
                        bin_counts[active_bank][sample_class][classified] + 1'b1;
                else
                    overflow[active_bank][sample_class] <= 1'b1;
                if (counts[active_bank][sample_class] != '1)
                    counts[active_bank][sample_class] <= counts[active_bank][sample_class] + 1'b1;
                else
                    overflow[active_bank][sample_class] <= 1'b1;
                if (new_sum[64]) begin
                    sums[active_bank][sample_class] <= '1;
                    overflow[active_bank][sample_class] <= 1'b1;
                end else begin
                    sums[active_bank][sample_class] <= new_sum[63:0];
                end
                if (sample_latency < minimums[active_bank][sample_class])
                    minimums[active_bank][sample_class] <= sample_latency;
                if (sample_latency > maximums[active_bank][sample_class])
                    maximums[active_bank][sample_class] <= sample_latency;
            end
            if (snapshot_valid && snapshot_ready) begin
                active_bank <= !active_bank;
                state <= HEADER;
                class_no <= '0;
                bin_no <= '0;
                cut_tick <= snapshot_tick;
            end else if (m_valid && m_ready) begin
                case (state)
                    HEADER: state <= CLASS_TOTAL;
                    CLASS_TOTAL: state <= CLASS_BOUNDS;
                    CLASS_BOUNDS: state <= BUCKET;
                    BUCKET: begin
                        if (!end_bin) begin
                            bin_no <= bin_no + 1'b1;
                        end else if (!end_class) begin
                            state <= CLASS_TOTAL;
                            class_no <= class_no + 1'b1;
                            bin_no <= '0;
                        end else begin
                            state <= CLEARING;
                            class_no <= '0;
                            bin_no <= '0;
                            frame_number <= frame_number + 1'b1;
                            if (completed != '1)
                                completed <= completed + 1'b1;
                        end
                    end
                    default: ;
                endcase
            end else if (state == CLEARING) begin
                bin_counts[closed_bank][class_no][bin_no] <= '0;
                if (bin_no == 0) begin
                    counts[closed_bank][class_no] <= '0;
                    sums[closed_bank][class_no] <= '0;
                    minimums[closed_bank][class_no] <= '1;
                    maximums[closed_bank][class_no] <= '0;
                    overflow[closed_bank][class_no] <= 1'b0;
                end
                if (!end_bin) begin
                    bin_no <= bin_no + 1'b1;
                end else if (!end_class) begin
                    class_no <= class_no + 1'b1;
                    bin_no <= '0;
                end else begin
                    state <= IDLE;
                end
            end
        end
    end
endmodule

`default_nettype wire
