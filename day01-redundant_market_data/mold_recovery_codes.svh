`ifndef MOLD_RECOVERY_CODES_SVH
`define MOLD_RECOVERY_CODES_SVH
`define MRG_ACCEPT     4'd0
`define MRG_FRAME      4'd1
`define MRG_LIMIT      4'd2
`define MRG_EMPTY      4'd3
`define MRG_SLOT       4'd4
`define MRG_CONTEXT    4'd5
`define MRG_EPOCH      4'd6
`define MRG_SESSION    4'd7
`define MRG_UNARMED    4'd8
`define MRG_TOKEN      4'd9
`define MRG_EXPIRED    4'd10
`define MRG_RANGE      4'd11
`define MRG_DUPLICATE  4'd12
`define MRG_OVERLAP    4'd13
`define MRG_CANCELLED  4'd14

`define MRG_SYNC       2'd0
`define MRG_ARM        2'd1
`define MRG_CANCEL     2'd2
`define MRG_INSPECT    2'd3

`define MRG_OK         4'd0
`define MRG_BAD_SLOT   4'd1
`define MRG_BAD_EPOCH  4'd2
`define MRG_DISABLED   4'd3
`define MRG_BAD_REQ    4'd4
`define MRG_BUSY       4'd5
`define MRG_SAME_REQ   4'd6
`define MRG_BAD_TOKEN  4'd7
`define MRG_EXHAUSTED  4'd8
`define MRG_NO_LEASE   4'd9

`define MRG_IDLE       3'd0
`define MRG_LIVE       3'd1
`define MRG_COMPLETE   3'd2
`define MRG_TIMED_OUT  3'd3
`define MRG_REVOKED    3'd4
`endif
