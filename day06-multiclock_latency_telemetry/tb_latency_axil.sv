`timescale 1ns/1ps

module tb_latency_axil;
    localparam integer AW = 12;
    localparam integer N_WRITES = 361;
    localparam integer N_READS = 397;
    logic clk = 0;
    always #5 clk = !clk;

    logic reset = 1;
    logic [AW-1:0] s_awaddr = '0, s_araddr = '0;
    logic s_awvalid = 0, s_arvalid = 0, s_wvalid = 0;
    wire s_awready, s_arready, s_wready;
    logic [31:0] s_wdata = '0;
    logic [3:0] s_wstrb = '0;
    logic s_bready = 0, s_rready = 0;
    wire s_bvalid, s_rvalid;
    wire [1:0] s_bresp, s_rresp;
    wire [31:0] s_rdata;
    wire req_valid, req_ready, req_write;
    wire [AW-1:0] req_addr;
    wire [31:0] req_data;
    wire [3:0] req_strobe;
    logic rsp_valid;
    wire rsp_ready;
    logic [31:0] rsp_data;
    logic [1:0] rsp_status;

    latency_axil #(.ADDRESS_W(AW)) dut (.*);

    logic active = 0, inflight = 0;
    logic [31:0] mem [0:63];
    logic [31:0] response_data, held_read_data;
    logic [1:0] response_code, held_write_status, held_read_status;
    logic [31:0] rnd = 32'hba23fd9a;
    logic [1:0] b_model [0:N_WRITES-1];
    logic [33:0] r_model [0:N_READS-1];
    logic stalled_request, stalled_write, stalled_read;
    logic [48:0] held_request;
    integer address_accepted = 0, data_accepted = 0, reads_accepted = 0;
    integer writes_issued = 0, reads_issued = 0;
    integer writes_retired = 0, reads_retired = 0;
    integer req_stalls = 0, b_stalls = 0, r_stalls = 0;
    integer delay_cycles = 0;
    integer full_duplex = 0;
    assign req_ready = active && !inflight && rnd[4:2] >= 2;
    assign rsp_valid = inflight && delay_cycles == 0;
    assign rsp_data = response_data;
    assign rsp_status = response_code;

    function automatic logic [31:0] advance(input logic [31:0] value);
        logic [31:0] mixed;
        begin
            mixed = value ^ (value << 13);
            mixed = mixed ^ (mixed >> 17);
            advance = mixed ^ (mixed << 5);
        end
    endfunction

    function automatic logic [AW-1:0] address(input integer index, input logic write_op);
        logic [31:0] mixed;
        begin
            mixed = advance(32'(index * 3291) + (write_op ? 32'd62833 : 32'd7569));
            address = {4'd0,mixed[5:0],2'b00};
            if (index % 19 == 0)
                address[0] = 1'b1;
            else if (index % 31 == 0)
                address[11] = 1'b1;
        end
    endfunction

    function automatic logic [31:0] wr_payload(input integer index);
        wr_payload = advance(32'h74a0fc33 ^ 32'(index * 131317));
    endfunction

    function automatic logic [3:0] byte_mask(input integer index);
        byte_mask = 4'(index ^ (index >> 3));
    endfunction

    function automatic logic [1:0] respond_status(input logic [AW-1:0] addr, input logic writing);
        if (addr[11:8] != 0 || addr[1:0] != 0)
            respond_status = 2'b11;
        else if (writing && addr[7:2] == 0)
            respond_status = 2'b10;
        else
            respond_status = 2'b00;
    endfunction

    always @(posedge clk) begin
        if (reset) begin
            inflight <= 0;
            stalled_request <= 0;
            stalled_write <= 0;
            stalled_read <= 0;
            delay_cycles <= 0;
            response_data <= '0;
            response_code <= '0;
            for (integer i = 0; i < 64; i++) mem[i] <= 32'(i * 75);
        end else if (active) begin
            if (s_awvalid && s_awready) address_accepted++;
            if (s_wvalid && s_wready) data_accepted++;
            if (s_arvalid && s_arready) reads_accepted++;
            if (stalled_request && (!req_valid || {req_write,req_addr,req_data,req_strobe} !== held_request))
                $fatal(1, "Register request changed before acceptance");
            if (stalled_write && (!s_bvalid || s_bresp !== held_write_status))
                $fatal(1, "Write response changed under stall");
            if (stalled_read && (!s_rvalid || {s_rresp,s_rdata} !== {held_read_status,held_read_data}))
                $fatal(1, "Read response changed under stall");

            stalled_request <= req_valid && !req_ready;
            stalled_write <= s_bvalid && !s_bready;
            stalled_read <= s_rvalid && !s_rready;
            if (req_valid && !req_ready) begin
                req_stalls++;
                held_request <= {req_write,req_addr,req_data,req_strobe};
            end
            if (s_bvalid && !s_bready) begin
                b_stalls++;
                held_write_status <= s_bresp;
            end
            if (s_rvalid && !s_rready) begin
                r_stalls++;
                held_read_status <= s_rresp;
                held_read_data <= s_rdata;
            end
            if (s_bvalid && s_rvalid) full_duplex++;

            if (inflight && delay_cycles != 0) delay_cycles <= delay_cycles - 1;
            if (rsp_valid && rsp_ready) inflight <= 0;
            if (req_valid && req_ready) begin
                inflight <= 1;
                delay_cycles <= int'(rnd[14:12]);
                response_code <= respond_status(req_addr, req_write);
                if (req_write) begin
                    if (writes_issued >= address_accepted || writes_issued >= data_accepted ||
                        {req_addr,req_data,req_strobe} !== {
                        address(writes_issued, 1),wr_payload(writes_issued),byte_mask(writes_issued)})
                        $fatal(1, "Address/data pairing failed at write %0d", writes_issued);
                    response_data <= '0;
                    b_model[writes_issued] = respond_status(req_addr, 1);
                    if (b_model[writes_issued] == 0)
                        for (integer lane = 0; lane < 4; lane++)
                            if (req_strobe[lane])
                                mem[req_addr[7:2]][8*lane+:8] <= req_data[8*lane+:8];
                    writes_issued++;
                end else begin
                    if (reads_issued >= reads_accepted || req_addr !== address(reads_issued, 0) ||
                        req_data != 0 || req_strobe != 0)
                        $fatal(1, "Read %0d reordered, dropped or carried write bits", reads_issued);
                    r_model[reads_issued][33:32] = respond_status(req_addr, 0);
                    r_model[reads_issued][31:0] = r_model[reads_issued][33:32] == 0 ?
                        mem[req_addr[7:2]] : '0;
                    response_data <= r_model[reads_issued][31:0];
                    reads_issued++;
                end
            end
            if (s_bvalid && s_bready) begin
                if (writes_retired >= writes_issued || s_bresp !== b_model[writes_retired])
                    $fatal(1, "Write retirement out of order at %0d", writes_retired);
                writes_retired++;
            end
            if (s_rvalid && s_rready) begin
                if (reads_retired >= reads_issued || {s_rresp,s_rdata} !== r_model[reads_retired])
                    $fatal(1, "Read retirement out of order at %0d", reads_retired);
                reads_retired++;
            end
        end
    end

    task automatic send_aw;
        logic [31:0] prng;
        integer pause;
        begin
            prng = 32'ha4d50003;
            for (integer i = 0; i < N_WRITES; i++) begin
                prng = advance(prng);
                pause = int'(prng[2:0]);
                repeat (pause+1) @(negedge clk);
                s_awaddr = address(i, 1);
                s_awvalid = 1;
                do @(posedge clk); while (!s_awready);
                @(negedge clk);
                s_awvalid = 0;
            end
        end
    endtask

    task automatic send_w;
        logic [31:0] prng;
        begin
            prng = 32'hdbf87344;
            for (integer i = 0; i < N_WRITES; i++) begin
                prng = advance(prng);
                repeat (int'(prng[3:0])+1) @(negedge clk);
                s_wdata = wr_payload(i);
                s_wstrb = byte_mask(i);
                s_wvalid = 1;
                do @(posedge clk); while (!s_wready);
                @(negedge clk);
                s_wvalid = 0;
            end
        end
    endtask

    task automatic send_ar;
        logic [31:0] prng;
        begin
            prng = 32'h82096755;
            for (integer i = 0; i < N_READS; i++) begin
                prng = advance(prng);
                repeat (int'(prng[1:0])+1) @(negedge clk);
                s_araddr = address(i, 0);
                s_arvalid = 1;
                do @(posedge clk); while (!s_arready);
                @(negedge clk);
                s_arvalid = 0;
            end
        end
    endtask

    always @(negedge clk) begin
        rnd = advance(rnd);
        s_bready = active && rnd[8:6] == 0;
        s_rready = active && rnd[18:16] < 2;
    end

    initial begin
        repeat (3) @(negedge clk);
        reset = 0;
        s_awaddr = 'h38;
        s_awvalid = 1;
        @(negedge clk);
        s_awvalid = 0;
        if (s_awready) $fatal(1, "Partial write address was not retained");
        reset = 1;
        repeat (3) @(negedge clk);
        reset = 0;
        #1;
        if (!s_awready || req_valid) $fatal(1, "Reset left a previous write half visible");
        active = 1;
        fork
            send_aw();
            send_w();
            send_ar();
        join
        wait (writes_retired == N_WRITES && reads_retired == N_READS);
        @(negedge clk);
        if (req_valid || rsp_valid || s_bvalid || s_rvalid)
            $fatal(1, "Unexpected extra bus traffic");
        if (!req_stalls || !b_stalls || !r_stalls || !full_duplex)
            $fatal(1, "Missing host or backend backpressure");
        $display("PASS AXI control: %0d writes, %0d reads, held %0d/%0d/%0d, concurrent responses %0d",
                 writes_retired, reads_retired, req_stalls, b_stalls, r_stalls, full_duplex);
        $finish;
    end
    initial begin #300000; $fatal(1, "AXI scoreboard timeout"); end
endmodule
