`ifndef MARKET_CODES_SVH
`define MARKET_CODES_SVH

`define MOP_ADD        4'd0
`define MOP_CANCEL     4'd1
`define MOP_EXECUTE    4'd2
`define MOP_DELETE     4'd3
`define MOP_REPLACE    4'd4
`define MOP_HEARTBEAT  4'd5
`define MOP_SNAP_BEGIN 4'd6
`define MOP_SNAP_ADD   4'd7
`define MOP_SNAP_END   4'd8
`define MOP_LEVEL      4'd9
`define MOP_ORDER      4'd10
`define MOP_BBO        4'd11
`define MOP_STAT       4'd12

`define MS_COLD        2'd0
`define MS_BOOT        2'd1
`define MS_LIVE        2'd2
`define MS_FENCED      2'd3

`define ME_OK            4'd0
`define ME_BANK          4'd1
`define ME_MODE          4'd2
`define ME_GAP           4'd3
`define ME_CODE          4'd4
`define ME_ID            4'd5
`define ME_PRICE         4'd6
`define ME_QTY           4'd7
`define ME_DUPLICATE     4'd8
`define ME_ABSENT        4'd9
`define ME_REPLACE_ID    4'd10
`define ME_STAT          4'd11

`define QR_QUOTING       3'd0
`define QR_INACTIVE      3'd1
`define QR_NO_SIDES      3'd2
`define QR_CROSSED       3'd3
`define QR_SPREAD        3'd4
`define QR_ZERO_SIZE     3'd5

`define STAT_COMMANDS    0
`define STAT_UPDATES     1
`define STAT_GAPS        2
`define STAT_FAULTS      3
`define STAT_TIMEOUTS    4
`define STAT_SNAPSHOTS   5
`define STAT_LIVE_ORDERS 6
`define STAT_AGE         7

`endif
