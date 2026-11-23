`timescale 1ns/1ps

module tb_frontend_pair_ram;
    localparam integer ADDRESS_BITS=5, DEPTH=1<<ADDRESS_BITS;
    logic clk=0;
    always #5 clk=!clk;
    logic write_enable=0, read_enable=0;
    logic [ADDRESS_BITS-1:0] write_address=0, read_address=0;
    logic [63:0] write_data=0;
    logic [127:0] read_pair;
    frontend_pair_ram #(.ADDRESS_BITS(ADDRESS_BITS)) dut (.*);

    logic [63:0] rows [0:DEPTH-1];
    logic initialized [0:DEPTH-1];
    logic reference_valid=0;
    logic [127:0] held_reference;
    logic [63:0] random_value=64'h760d1f4a79b827b5;
    integer reads=0, writes=0, dual=0, wrapped=0, held=0, odd=0, even=0;
    integer cycle=0;

    function automatic logic [63:0] advance(input logic [63:0] value);
        logic [63:0] stage;
        begin
            stage=value^(value<<13);
            stage=stage^(stage>>7);
            advance=stage^(stage<<17);
        end
    endfunction

    function automatic integer successor(input logic [ADDRESS_BITS-1:0] address);
        successor=(int'(address)+1)%DEPTH;
    endfunction

    always @(posedge clk) begin
        cycle=cycle+1;
        if (read_enable) begin
            if (!initialized[read_address] || !initialized[successor(read_address)])
                $fatal(1,"testbench read uninitialized memory");
            if (write_enable && (write_address == read_address ||
                int'(write_address) == successor(read_address)))
                $fatal(1,"testbench programmed a same-address read/write collision");
            held_reference={rows[successor(read_address)],rows[read_address]};
            reference_valid=1;
            reads=reads+1;
            if (read_address[0])
                odd=odd+1;
            else
                even=even+1;
            if (int'(read_address) == DEPTH-1)
                wrapped=wrapped+1;
        end else if (reference_valid) begin
            held=held+1;
        end
        if (write_enable) begin
            rows[write_address]=write_data;
            initialized[write_address]=1;
            writes=writes+1;
        end
        if (read_enable && write_enable)
            dual=dual+1;
        #1;
        if (reference_valid && read_pair !== held_reference)
            $fatal(1,"pair RAM mismatch cycle %0d address %0d: got %h expected %h",
                   cycle, read_address, read_pair, held_reference);
    end

    task automatic fill_all;
        begin
            for (integer address=0; address<DEPTH; address=address+1) begin
                @(negedge clk);
                write_enable=1;
                read_enable=0;
                write_address=ADDRESS_BITS'(address);
                random_value=advance(random_value);
                write_data=random_value;
            end
            @(negedge clk);
            write_enable=0;
        end
    endtask

    task automatic read_both_directions;
        begin
            for (integer index=0; index<DEPTH; index=index+1) begin
                @(negedge clk);
                read_enable=1;
                read_address=ADDRESS_BITS'(index);
            end
            for (integer index=DEPTH-1; index>=0; index=index-1) begin
                @(negedge clk);
                read_enable=1;
                read_address=ADDRESS_BITS'(index);
            end
            @(negedge clk);
            read_enable=0;
        end
    endtask

    initial begin
        for (integer i=0; i<DEPTH; i=i+1)
            initialized[i]=0;
        fill_all();
        read_both_directions();
        for (integer testcase=0; testcase<3500; testcase=testcase+1) begin
            @(negedge clk);
            random_value=advance(random_value);
            read_enable=random_value[0] || random_value[2];
            read_address=random_value[ADDRESS_BITS:1];
            write_enable=random_value[33] || random_value[34];
            write_address=random_value[32+ADDRESS_BITS:33];
            if (read_enable && write_enable && (write_address == read_address ||
                int'(write_address) == successor(read_address)))
                write_address=write_address+ADDRESS_BITS'(3);
            random_value=advance(random_value);
            write_data=random_value ^ 64'(testcase);
        end
        @(negedge clk);
        read_enable=0;
        write_enable=0;
        read_both_directions();
        if (reads<2500 || writes<2400 || dual<1500 || wrapped<40 ||
            held<700 || odd<1000 || even<1000)
            $fatal(1,"pair RAM coverage incomplete");
        $display("PASS pair RAM: %0d paired reads, %0d writes, %0d simultaneous accesses, %0d wrap pairs",
                 reads,writes,dual,wrapped);
        $finish;
    end
endmodule
