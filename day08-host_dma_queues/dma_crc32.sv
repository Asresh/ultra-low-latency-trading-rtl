`timescale 1ns/1ps
`default_nettype none

module dma_crc32 (
    input  wire          clk,
    input  wire          reset,
    input  wire          seed,
    input  wire          take,
    input  wire [63:0]   data,
    input  wire [7:0]    keep,
    output wire [31:0]   digest
);
    logic [31:0] state;
    logic [31:0] updated;

    always @* begin
        updated = state;
        for (integer lane = 0; lane < 8; lane++) begin
            if (keep[lane]) begin
                for (integer bit_index = 0; bit_index < 8; bit_index++) begin
                    if (updated[0] ^ data[8*lane+bit_index])
                        updated = (updated >> 1) ^ 32'hedb8_8320;
                    else
                        updated = updated >> 1;
                end
            end
        end
    end

    assign digest = ~state;
    always_ff @(posedge clk) begin
        if (reset || seed)
            state <= '1;
        else if (take)
            state <= updated;
    end
endmodule

`default_nettype wire
