`timescale 1ns/1ps

module tb_eth_reset_recovery;
    logic clk=0;
    always #5 clk=!clk;
    logic rst=1;
    logic [63:0] now=0;

    logic desc_valid=0, desc_ready;
    logic [319:0] description='0;
    logic s_valid=0, s_ready;
    logic [63:0] s_data=0;
    logic [7:0] s_keep=0;
    logic s_first=0, s_last=0;
    logic tx_valid, tx_ready;
    logic [63:0] tx_data;
    logic [7:0] tx_keep;
    logic tx_first, tx_last;
    logic done_valid, done_ready=0;
    logic [127:0] done_data;

    logic loopback_enabled=0;
    logic inject_valid=0, inject_first=0, inject_last=0;
    logic [63:0] inject_data=0;
    logic rx_ready;
    logic inspect_valid, inspect_take=0;
    logic [383:0] inspection;
    logic rx_valid, rx_first, rx_last;
    logic [63:0] rx_data;
    logic [7:0] rx_keep;
    logic rx_payload_ready=0;

    integer cycle=0, wire_beats=0, completions=0, released=0, ingress_beats=0;
    integer inspected=0;

    eth_tx_bank #(.MAX_PAYLOAD(64)) transmitter (
        .clk, .rst, .timestamp(now), .desc_valid, .desc_ready, .desc_data(description),
        .s_valid, .s_ready, .s_data, .s_keep, .s_first, .s_last,
        .m_valid(tx_valid), .m_ready(tx_ready), .m_data(tx_data), .m_keep(tx_keep),
        .m_first(tx_first), .m_last(tx_last), .done_valid, .done_ready, .done_data
    );

    assign tx_ready=loopback_enabled && rx_ready;
    eth_rx_bank #(.MAX_FRAME(128)) receiver (
        .clk, .rst, .timestamp(now),
        .mac_valid(loopback_enabled ? tx_valid : inject_valid), .mac_ready(rx_ready),
        .mac_data(loopback_enabled ? tx_data : inject_data),
        .mac_keep(loopback_enabled ? tx_keep : 8'hff),
        .mac_first(loopback_enabled ? tx_first : inject_first),
        .mac_last(loopback_enabled ? tx_last : inject_last), .mac_bad(1'b0),
        .inspect_valid, .inspect_take, .inspect_allow(1'b1), .inspect_data(inspection),
        .m_valid(rx_valid), .m_ready(rx_payload_ready), .m_data(rx_data),
        .m_keep(rx_keep), .m_first(rx_first), .m_last(rx_last)
    );

    always @(posedge clk) begin
        cycle=cycle+1;
        if (cycle > 2500)
            $fatal(1,"recovery smoke timed out");
        if (rst)
            now <= 64'd0;
        else begin
            now <= now+64'd1;
            if (tx_valid && tx_ready)
                wire_beats=wire_beats+1;
            if (done_valid && done_ready) begin
                if (done_data[127:120] != 0 || done_data[111:80] != 32'h55aa3322)
                    $fatal(1,"pre-reset TX status leaked");
                completions=completions+1;
            end
            if (rx_ready && (loopback_enabled ? tx_valid : inject_valid))
                ingress_beats=ingress_beats+1;
            if (inspect_valid && inspect_take) begin
                if (inspection[383:376] != 0 || inspection[95:80] != 3 ||
                    inspection[111:96] != 18000 || inspection[7:6] != 2)
                    $fatal(1,"fresh RX packet misparsed after flush");
                inspected=inspected+1;
            end
            if (rx_valid && rx_payload_ready) begin
                if (!rx_first || !rx_last || rx_keep != 8'h07 || rx_data != 64'h0000000000ccbb11)
                    $fatal(1,"fresh RX payload contains old buffered data");
                released=released+1;
            end
        end
    end

    task automatic offer_description(input logic [31:0] cookie, input logic [15:0] bytes);
        begin
            @(negedge clk);
            description={
                cookie, 48'h01005e010203, 48'h022022334455,
                32'h0a0f0409, 32'hef010203,16'd12000,16'd18000,bytes,
                16'h8186,16'h2362,16'h0908,8'd64,8'h38,8'd0,2'd2,1'b0,1'b1,4'd0
            };
            desc_valid=1;
            do @(posedge clk); while (!desc_ready);
            @(negedge clk);
            desc_valid=0;
        end
    endtask

    task automatic send_piece(input logic [63:0] data, input logic [7:0] keep, input logic last);
        begin
            @(negedge clk);
            s_valid=1;
            s_data=data;
            s_keep=keep;
            s_first=1;
            s_last=last;
            do @(posedge clk); while (!s_ready);
            @(negedge clk);
            s_valid=0;
        end
    endtask

    task automatic pulse_reset;
        begin
            @(negedge clk);
            rst=1;
            #1;
            if (tx_valid || done_valid || inspect_valid || rx_valid || desc_ready || rx_ready)
                $fatal(1,"one of the stream interfaces remained active during reset");
            repeat (3) @(negedge clk);
            rst=0;
            #1;
            if (!desc_ready || !rx_ready || tx_valid || done_valid || inspect_valid || rx_valid)
                $fatal(1,"banks did not return empty after reset");
        end
    endtask

    initial begin
        repeat (3) @(negedge clk);
        rst=0;
        offer_description(32'hdead0001,16);
        send_piece(64'h123456789abcdef0,8'hff,0);
        @(negedge clk);
        inject_valid=1;
        inject_first=1;
        inject_data=64'h45abccddeeff1001;
        @(posedge clk);
        @(negedge clk);
        inject_valid=0;
        pulse_reset();

        offer_description(32'hdead0002,0);
        while (!tx_valid) @(negedge clk);
        if (!tx_first || tx_ready)
            $fatal(1,"expected an unaccepted TX first beat before reset");
        pulse_reset();

        offer_description(32'h55aa3322,3);
        loopback_enabled=1;
        inspect_take=1;
        rx_payload_ready=1;
        done_ready=1;
        send_piece(64'hfedcba9876ccbb11,8'h07,1);
        while (released != 1) @(negedge clk);
        repeat (10) @(negedge clk);
        if (wire_beats != 8 || ingress_beats != 9 || inspected != 1 || completions != 1)
            $fatal(1,"reset produced duplicated or missing loopback packet");
        $display("PASS reset recovery: partial RX/TX capture and a stalled TX SOP flushed cleanly");
        $finish;
    end
endmodule
