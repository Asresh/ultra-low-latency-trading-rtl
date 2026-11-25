`ifndef HOST_DMA_PROTOCOL_SVH
`define HOST_DMA_PROTOCOL_SVH
`define DMA_PULL  8'h01
`define DMA_PUSH  8'h02
`define DMA_FENCE 8'h03
`define DMA_READBACK 8'h04
`define DMA_OK       8'd0
`define DMA_BAD_OP   8'd1
`define DMA_BAD_DESC 8'd2
`define DMA_ADDRESS  8'd3
`define DMA_READ     8'd4
`define DMA_WRITE    8'd5
`define DMA_CRC      8'd6
`define DMA_FRAME    8'd7
`define DMA_TIMEOUT  8'd8
`define DMA_VERIFY   8'd9
`endif
