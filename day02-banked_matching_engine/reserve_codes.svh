`ifndef MATCHING_RESERVE_CODES_SVH
`define MATCHING_RESERVE_CODES_SVH

`define RM_NEW            3'd0
`define RM_CANCEL         3'd1
`define RM_REDUCE         3'd2
`define RM_LEVEL          3'd3
`define RM_ORDER          3'd4
`define RM_SNAPSHOT       3'd5
`define RM_IMPORT         3'd6
`define RM_IMPORT_BEGIN   2'd0
`define RM_IMPORT_ROW     2'd1
`define RM_IMPORT_COMMIT  2'd2
`define RM_IMPORT_ABORT   2'd3

`define RM_GTC            2'd0
`define RM_IOC            2'd1
`define RM_POST_ONLY      2'd2
`define RM_FOK            2'd3

`define RM_STP_NONE       2'd0
`define RM_STP_MAKER      2'd1
`define RM_STP_TAKER      2'd2

`define RM_EV_FILL        4'd0
`define RM_EV_REST        4'd1
`define RM_EV_CANCEL      4'd2
`define RM_EV_REJECT      4'd3
`define RM_EV_REDUCE      4'd4
`define RM_EV_EXPIRE      4'd5
`define RM_EV_LEVEL       4'd6
`define RM_EV_ORDER       4'd7
`define RM_EV_STP_MAKER   4'd8
`define RM_EV_STP_TAKER   4'd9
`define RM_EV_SNAPSHOT    4'd10
`define RM_EV_IMPORT      4'd11

`define RM_ERR_ID         4'd1
`define RM_ERR_PRICE      4'd2
`define RM_ERR_QTY        4'd3
`define RM_ERR_DUPLICATE  4'd4
`define RM_ERR_ABSENT     4'd5
`define RM_ERR_POLICY     4'd6
`define RM_ERR_POST       4'd7
`define RM_ERR_REDUCE     4'd8
`define RM_ERR_BANK       4'd9
`define RM_ERR_OWNER      4'd10
`define RM_ERR_FOK        4'd11
`define RM_ERR_OPCODE     4'd12
`define RM_ERR_DISPLAY    4'd13
`define RM_ERR_IMPORT     4'd14
`define RM_ERR_CROSSED    4'd15

`endif
