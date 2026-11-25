`ifndef CALL_AUCTION_CODES_SVH
`define CALL_AUCTION_CODES_SVH

`define AU_OPEN      3'd0
`define AU_ADD       3'd1
`define AU_CANCEL    3'd2
`define AU_REDUCE    3'd3
`define AU_INDICATE  3'd4
`define AU_CLOSE     3'd5
`define AU_ABORT     3'd6
`define AU_INSPECT   3'd7

`define AU_EV_REJECT      4'd0
`define AU_EV_OPENED      4'd1
`define AU_EV_ADDED       4'd2
`define AU_EV_CANCELED    4'd3
`define AU_EV_REDUCED     4'd4
`define AU_EV_INSPECTED   4'd5
`define AU_EV_INDICATIVE  4'd6
`define AU_EV_CLEARING    4'd7
`define AU_EV_TRADE       4'd8
`define AU_EV_CARRY       4'd9
`define AU_EV_EXPIRE      4'd10
`define AU_EV_DONE        4'd11
`define AU_EV_ABORTED     4'd12

`define AU_ERR_NONE       4'd0
`define AU_ERR_PHASE      4'd1
`define AU_ERR_EPOCH      4'd2
`define AU_ERR_FIELDS     4'd3
`define AU_ERR_PRICE      4'd4
`define AU_ERR_ID         4'd5
`define AU_ERR_DUPLICATE  4'd6
`define AU_ERR_ABSENT     4'd7
`define AU_ERR_OWNER      4'd8
`define AU_ERR_QUANTITY   4'd9

`endif
