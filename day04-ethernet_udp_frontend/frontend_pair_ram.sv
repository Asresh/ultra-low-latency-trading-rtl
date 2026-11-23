`timescale 1ns/1ps

module frontend_pair_ram #(
    parameter integer ADDRESS_BITS = 8
) (
    input  logic                    clk,
    input  logic                    write_enable,
    input  logic [ADDRESS_BITS-1:0] write_address,
    input  logic [63:0]             write_data,
    input  logic                    read_enable,
    input  logic [ADDRESS_BITS-1:0] read_address,
    output logic [127:0]            read_pair
);
    localparam integer BANK_DEPTH = 1 << (ADDRESS_BITS-1);
    logic [63:0] even_words [0:BANK_DEPTH-1];
    logic [63:0] odd_words [0:BANK_DEPTH-1];
    logic [63:0] even_value, odd_value;
    logic [ADDRESS_BITS-2:0] even_address, odd_address;
    logic align_odd;

    assign odd_address = read_address[ADDRESS_BITS-1:1];
    assign even_address = odd_address + {{ADDRESS_BITS-2{1'b0}}, read_address[0]};

    always_ff @(posedge clk) begin
        if (write_enable && !write_address[0])
            even_words[write_address[ADDRESS_BITS-1:1]] <= write_data;
        if (read_enable)
            even_value <= even_words[even_address];
    end

    always_ff @(posedge clk) begin
        if (write_enable && write_address[0])
            odd_words[write_address[ADDRESS_BITS-1:1]] <= write_data;
        if (read_enable)
            odd_value <= odd_words[odd_address];
    end

    always_ff @(posedge clk)
        if (read_enable)
            align_odd <= read_address[0];

    assign read_pair = align_odd ? {even_value, odd_value} : {odd_value, even_value};
endmodule
