`timescale 1ns/1ps
`default_nettype none

module ouch_rx_decode (
    input ouch_wire_pkg::packet_t frame,
    output ouch_wire_pkg::inbound_t decoded
);
    import ouch_wire_pkg::*;

    inbound_t message;
    frame_length_t size;
    body_t data;
    logic [7:0] soup_type;
    logic [7:0] message_type;
    logic [7:0] core_size;
    logic has_appendage;
    logic appendage_optional;
    logic [15:0] appendage;

    always @* begin
        size = frame.length;
        data = frame.body;
        soup_type = data[0 +: 8];
        message_type = data[8 +: 8];
        message = '0;
        message.soup_type = soup_type;
        message.status = ST_OK;
        core_size = 0;
        has_appendage = 1'b0;
        appendage_optional = 1'b0;
        appendage = 0;
        case (soup_type)
            "A": begin
                if (size != 31) message.status = ST_FRAME_LENGTH;
                message.session = swap80(data[1*8 +: 80]);
                message.ascii_sequence = swap160(data[11*8 +: 160]);
            end
            "J": begin
                if (size != 2) message.status = ST_FRAME_LENGTH;
                message.reason = {8'h00, data[1*8 +: 8]};
            end
            "H", "Z": if (size != 1) message.status = ST_FRAME_LENGTH;
            "+": begin end
            "S": begin
                message.ouch_type = message_type;
                message.timestamp = swap64(data[2*8 +: 64]);
                message.order_ref = swap32(data[10*8 +: 32]);
                case (message_type)
                    "S": begin
                        core_size = 11;
                        message.reason = {8'h00, data[10*8 +: 8]};
                        message.order_ref = 0;
                    end
                    "A": begin
                        core_size = 63;
                        has_appendage = 1'b1;
                        appendage = data[63*8 +: 16];
                        message.side = data[14*8 +: 8];
                        message.quantity = swap32(data[15*8 +: 32]);
                        message.symbol = swap64(data[19*8 +: 64]);
                        message.price = swap64(data[27*8 +: 64]);
                        message.venue_id = swap64(data[37*8 +: 64]);
                        message.order_state = data[48*8 +: 8];
                        message.client_id = swap112(data[49*8 +: 112]);
                    end
                    "U": begin
                        core_size = 67;
                        has_appendage = 1'b1;
                        appendage = data[67*8 +: 16];
                        message.previous_order_ref = message.order_ref;
                        message.order_ref = swap32(data[14*8 +: 32]);
                        message.side = data[18*8 +: 8];
                        message.quantity = swap32(data[19*8 +: 32]);
                        message.symbol = swap64(data[23*8 +: 64]);
                        message.price = swap64(data[31*8 +: 64]);
                        message.venue_id = swap64(data[41*8 +: 64]);
                        message.order_state = data[52*8 +: 8];
                        message.client_id = swap112(data[53*8 +: 112]);
                    end
                    "C": begin
                        core_size = 19;
                        has_appendage = 1'b1;
                        appendage_optional = 1'b1;
                        appendage = data[19*8 +: 16];
                        message.quantity = swap32(data[14*8 +: 32]);
                        message.reason = {8'h00, data[18*8 +: 8]};
                    end
                    "E": begin
                        core_size = 35;
                        has_appendage = 1'b1;
                        appendage = data[35*8 +: 16];
                        message.quantity = swap32(data[14*8 +: 32]);
                        message.price = swap64(data[18*8 +: 64]);
                        message.reason = {8'h00, data[26*8 +: 8]};
                        message.venue_id = swap64(data[27*8 +: 64]);
                    end
                    "J": begin
                        core_size = 30;
                        has_appendage = 1'b1;
                        appendage_optional = 1'b1;
                        appendage = data[30*8 +: 16];
                        message.reason = {data[14*8 +: 8], data[15*8 +: 8]};
                        message.client_id = swap112(data[16*8 +: 112]);
                    end
                    "P", "I": begin
                        core_size = 14;
                        has_appendage = 1'b1;
                        appendage_optional = 1'b1;
                        appendage = data[14*8 +: 16];
                    end
                    default: message.status = ST_OUCH_TYPE;
                endcase
                if (size < 2) begin
                    message.status = ST_OUCH_LENGTH;
                end else if (message.status == ST_OK) begin
                    if (has_appendage && 8'(size) == core_size + 2) begin
                        if (appendage != 0) message.status = ST_OUCH_APPENDAGE;
                    end else if (8'(size) != core_size || (has_appendage && !appendage_optional)) begin
                        message.status = ST_OUCH_LENGTH;
                    end
                end
            end
            default: message.status = ST_SOUP_TYPE;
        endcase
    end

    assign decoded = message;
endmodule

`default_nettype wire
