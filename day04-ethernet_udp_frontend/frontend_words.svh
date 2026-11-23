function automatic logic [3:0] prefix_bytes(input logic [7:0] keep);
    case (keep)
        8'h01: prefix_bytes = 4'd1;
        8'h03: prefix_bytes = 4'd2;
        8'h07: prefix_bytes = 4'd3;
        8'h0f: prefix_bytes = 4'd4;
        8'h1f: prefix_bytes = 4'd5;
        8'h3f: prefix_bytes = 4'd6;
        8'h7f: prefix_bytes = 4'd7;
        8'hff: prefix_bytes = 4'd8;
        default: prefix_bytes = 4'd0;
    endcase
endfunction

function automatic logic [63:0] wire_big(input logic [63:0] lane_data);
    for (integer lane = 0; lane < 8; lane = lane+1)
        wire_big[63-8*lane -: 8] = lane_data[8*lane +: 8];
endfunction

function automatic logic [31:0] sum_word(
    input logic [31:0] previous, input logic [63:0] lanes, input logic [3:0] length
);
    logic [15:0] a, b, c, d;
    begin
        a = {length > 0 ? lanes[7:0] : 8'd0,   length > 1 ? lanes[15:8] : 8'd0};
        b = {length > 2 ? lanes[23:16] : 8'd0, length > 3 ? lanes[31:24] : 8'd0};
        c = {length > 4 ? lanes[39:32] : 8'd0, length > 5 ? lanes[47:40] : 8'd0};
        d = {length > 6 ? lanes[55:48] : 8'd0, length > 7 ? lanes[63:56] : 8'd0};
        sum_word = previous + {16'd0,a} + {16'd0,b} + {16'd0,c} + {16'd0,d};
    end
endfunction

function automatic logic [15:0] fold_sum(input logic [31:0] accumulator);
    logic [16:0] folded;
    begin
        folded = {1'b0, accumulator[31:16]} + {1'b0, accumulator[15:0]};
        fold_sum = folded[15:0] + {15'd0, folded[16]};
    end
endfunction

function automatic logic [31:0] pseudo_sum(
    input logic [31:0] source_addr,
    input logic [31:0] destination_addr,
    input logic [15:0] udp_total
);
    begin
        pseudo_sum = {16'd0, source_addr[31:16]} + {16'd0, source_addr[15:0]}
                   + {16'd0, destination_addr[31:16]} + {16'd0, destination_addr[15:0]}
                   + 32'd17 + {16'd0, udp_total};
    end
endfunction
