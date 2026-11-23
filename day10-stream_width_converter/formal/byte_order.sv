`default_nettype none

// The proof-side queue shifts data instead of using modulo-indexed storage.
module formal_byte_order(input wire clk);
    (* anyseq *) logic rst, s_valid, s_last, m_ready;
    (* anyseq *) logic [7:0] s_data;
    wire s_ready, m_valid, m_last;
    wire [15:0] m_data, m_tag;
    wire [1:0] m_keep, occupancy;
    wire s_keep = 1;
    wire [15:0] s_tag = 16'hA1F8;

    streaming_gearbox #(.IN_BYTES(1), .OUT_BYTES(2), .CAPACITY(3)) dut (.*);

    logic [23:0] model_bytes = '0;
    logic [2:0] model_ends = '0;
    logic [1:0] count = '0;
    logic past_valid = 0;
    logic seen_stall = 0, seen_partial = 0;

    wire available = count >= 2 || (count != 0 && model_ends[0]);
    wire one_byte = count != 0 && model_ends[0];
    wire take = s_valid && s_ready;
    wire retire = m_valid && m_ready;
    wire [1:0] popped = retire ? (one_byte ? 2'(1) : 2'(2)) : 2'(0);
    wire [1:0] left = count - popped;

    always @(posedge clk) begin : reference_queue
        logic [23:0] shifted_bytes;
        logic [2:0] shifted_ends;
        past_valid <= 1;
        if (!past_valid) assume(rst);
        if (rst) begin
            assert(!s_ready && !m_valid);
            count <= 0;
            model_bytes <= 0;
            model_ends <= 0;
            seen_stall <= 0;
            seen_partial <= 0;
        end else begin
            if (past_valid) begin
                // Independent FIFO content and packet-boundary checks at the pins.
                assert(occupancy == count);
                assert(s_ready == (count < 3));
                assert(m_valid == available);
                if (available) begin
                    assert(m_tag == s_tag);
                    assert(m_keep == (one_byte ? 2'b01 : 2'b11));
                    assert(m_data[7:0] == model_bytes[7:0]);
                    assert(m_data[15:8] == (one_byte ? 8'b0 : model_bytes[15:8]));
                    assert(m_last == (one_byte || model_ends[1]));
                end
                if ($past(!rst && m_valid && !m_ready)) begin
                    assert(m_valid);
                    assert(m_keep == $past(m_keep));
                    assert(m_data == $past(m_data));
                    assert(m_last == $past(m_last));
                end
            end
            shifted_bytes = model_bytes >> (8 * popped);
            shifted_ends = model_ends >> popped;
            if (take) begin
                shifted_bytes[8*left+:8] = s_data;
                shifted_ends[left] = s_last;
            end
            model_bytes <= shifted_bytes;
            model_ends <= shifted_ends;
            count <= left + 2'(take);
            if (m_valid && !m_ready)
                seen_stall <= 1;
            if (retire && one_byte)
                seen_partial <= 1;
            cover(take && retire && !m_last);
            cover(seen_stall && seen_partial && retire);
            cover(occupancy == 3 && !s_ready && s_valid);
        end
    end
endmodule

`default_nettype wire
