`timescale 1ns/1ps
`default_nettype none

module latency_counter_bank (
    input  wire          clk,
    input  wire          reset,
    input  wire          clear,
    input  wire          accepted,
    input  wire [2:0]    status,
    output wire [255:0]  statistics
);
    logic [31:0] count [0:7];

    for (genvar status_no = 0; status_no < 8; status_no = status_no + 1) begin : g_counter
        assign statistics[32*status_no +: 32] = count[status_no];
        always_ff @(posedge clk) begin
            if (reset || clear)
                count[status_no] <= '0;
            else if (accepted && status == 3'(status_no) && count[status_no] != '1)
                count[status_no] <= count[status_no] + 1'b1;
        end
    end
endmodule

`default_nettype wire
