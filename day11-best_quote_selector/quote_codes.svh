`ifndef QUOTE_CODES_SVH
`define QUOTE_CODES_SVH

`define QUOTE_REPLACE      3'd0
`define QUOTE_WITHDRAW     3'd1
`define QUOTE_ENABLE       3'd2
`define QUOTE_DISABLE      3'd3

`define QUOTE_OK           4'd0
`define QUOTE_BAD_INST     4'd1
`define QUOTE_BAD_VENUE    4'd2
`define QUOTE_BAD_OP       4'd3
`define QUOTE_DISABLED     4'd4
`define QUOTE_BAD_SEQ      4'd5
`define QUOTE_BAD_PRICE    4'd6
`define QUOTE_BAD_AGE      4'd7

`define BOOK_EMPTY         3'd0
`define BOOK_BID_ONLY      3'd1
`define BOOK_ASK_ONLY      3'd2
`define BOOK_OPEN          3'd3
`define BOOK_LOCKED        3'd4
`define BOOK_CROSSED       3'd5

`endif
