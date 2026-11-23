`timescale 1ns/1ps

// Two parity banks supply adjacent words on one synchronous read.
module mold_pair_ram #(
    parameter integer ADDR_BITS = 8
) (
    input  logic                 clk,
    input  logic                 write_enable,
    input  logic [ADDR_BITS-1:0] write_word,
    input  logic [63:0]          write_data,
    input  logic                 read_enable,
    input  logic [ADDR_BITS-1:0] read_word,
    output logic [127:0]         read_pair
);
    localparam integer PARITY_DEPTH = 1 << (ADDR_BITS - 1);

    logic [63:0] even_mem [0:PARITY_DEPTH-1];
    logic [63:0] odd_mem [0:PARITY_DEPTH-1];
    logic [ADDR_BITS-2:0] even_address, odd_address;
    logic [63:0] even_data, odd_data;
    logic first_is_odd;

    assign odd_address = read_word[ADDR_BITS-1:1];
    assign even_address = odd_address + {{ADDR_BITS-2{1'b0}}, read_word[0]};

    always_ff @(posedge clk) begin
        if (write_enable && !write_word[0])
            even_mem[write_word[ADDR_BITS-1:1]] <= write_data;
        if (read_enable)
            even_data <= even_mem[even_address];
    end

    always_ff @(posedge clk) begin
        if (write_enable && write_word[0])
            odd_mem[write_word[ADDR_BITS-1:1]] <= write_data;
        if (read_enable)
            odd_data <= odd_mem[odd_address];
    end

    always_ff @(posedge clk) begin
        if (read_enable)
            first_is_odd <= read_word[0];
    end

    assign read_pair = first_is_odd ? {even_data, odd_data} : {odd_data, even_data};
endmodule
