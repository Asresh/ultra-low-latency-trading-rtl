`ifndef BANKED_MATCHING_CODES_SVH
`define BANKED_MATCHING_CODES_SVH
`define OP_NEW     2'd0
`define OP_CANCEL  2'd1
`define OP_REDUCE  2'd2
`define OP_LEVEL   2'd3
`define TIF_GTC    2'd0
`define TIF_IOC    2'd1
`define TIF_POST   2'd2
`define EV_FILL    3'd0
`define EV_REST    3'd1
`define EV_CANCEL  3'd2
`define EV_REJECT  3'd3
`define EV_REDUCE  3'd4
`define EV_EXPIRE  3'd5
`define EV_LEVEL   3'd6
`define ERR_ID     4'd1
`define ERR_PRICE  4'd2
`define ERR_QTY    4'd3
`define ERR_DUP    4'd4
`define ERR_ABSENT 4'd5
`define ERR_TIF    4'd6
`define ERR_POST   4'd7
`define ERR_REDUCE 4'd8
`define ERR_BANK   4'd9
`endif
