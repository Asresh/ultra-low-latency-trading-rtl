`ifndef RECONCILE_DEFS_SVH
`define RECONCILE_DEFS_SVH

`define DC_KEY_W 74
`define DC_PAYLOAD_W 89
`define DC_STAGED 4'd0
`define DC_MATCH 4'd1
`define DC_PAIR_CONFLICT 4'd2
`define DC_DUPLICATE 4'd3
`define DC_DUP_CONFLICT 4'd4
`define DC_MISSING_COPY 4'd5
`define DC_MISSING_PRIMARY 4'd6
`define DC_LATE 4'd7
`define DC_FULL 4'd8
`define DC_STALE_EPOCH 4'd9
`define DC_BAD_EVENT 4'd10
`define DC_RESTART 4'd11
`define DC_BAD_RESTART 4'd12

`endif
