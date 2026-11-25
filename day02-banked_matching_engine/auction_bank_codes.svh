`ifndef AUCTION_BANK_CODES_SVH
`define AUCTION_BANK_CODES_SVH

`define AB_ARM    2'd0
`define AB_CANCEL 2'd1
`define AB_QUERY  2'd2

`define AB_ADMIN_OK       2'd0
`define AB_ADMIN_FIELDS   2'd1
`define AB_ADMIN_ROLLBACK 2'd2

`define AB_READY     3'd0
`define AB_INACTIVE  3'd1
`define AB_EPOCH     3'd2
`define AB_BUSY      3'd3
`define AB_ARMED     3'd4
`define AB_UNARMED   3'd5
`define AB_PAST      3'd6

`define AB_ADMITTED    2'd0
`define AB_INVALID_KEY 2'd1
`define AB_LOCKED      2'd2
`define AB_CUTOFF      2'd3

`endif
