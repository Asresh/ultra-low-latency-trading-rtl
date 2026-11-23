`ifndef FRONTEND_CODES_SVH
`define FRONTEND_CODES_SVH
`define FE_OK            8'd0
`define FE_BAD_AXIS      8'd1
`define FE_MAC_BAD       8'd2
`define FE_FRAME_LIMIT   8'd3
`define FE_L2_TYPE       8'd4
`define FE_VLAN_LIMIT    8'd5
`define FE_L2_SHORT      8'd6
`define FE_IP_SHAPE      8'd7
`define FE_IP_LENGTH     8'd8
`define FE_IP_FRAGMENT   8'd9
`define FE_IP_CHECKSUM   8'd10
`define FE_IP_PROTOCOL   8'd11
`define FE_UDP_LENGTH    8'd12
`define FE_UDP_CHECKSUM  8'd13
`define FE_UDP_ZERO      8'd14
`define FE_RULE_MISS     8'd15
`define FE_RULE_DROP     8'd16
`define FE_TX_DESCRIPTOR 8'd17
`define FE_TX_LENGTH     8'd18
`define FE_RX_META_BITS 384
`define FE_TX_DESC_BITS 320
`define FE_TX_DONE_BITS 128
`define FE_RULE_BITS 192
`endif
