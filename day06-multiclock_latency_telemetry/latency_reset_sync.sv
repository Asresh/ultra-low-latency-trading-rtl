`timescale 1ns/1ps
`default_nettype none

module latency_reset_sync (
    input  wire clk,
    input  wire power_reset,
    output wire reset
);
    (* ASYNC_REG = "TRUE" *) logic [1:0] pipe;

    always_ff @(posedge clk or posedge power_reset) begin
        if (power_reset)
            pipe <= '1;
        else
            pipe <= {pipe[0], 1'b0};
    end

    assign reset = pipe[1];
endmodule

`default_nettype wire
