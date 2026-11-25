`timescale 1ns/1ps
`default_nettype none

package ouch_wire_pkg;
    localparam integer MAX_BODY = 72;
    localparam integer BODY_BITS = 8 * MAX_BODY;
    localparam integer LENGTH_BITS = $clog2(MAX_BODY + 1);
    localparam logic [7:0] NO_SLOT = 8'hff;
    localparam logic [31:0] MAX_SHARES = 999_999;
    localparam logic [63:0] MAX_LIMIT_PRICE = 1_999_999_900;
    localparam logic [79:0] BLANK_SESSION = 80'h20202020202020202020;

    typedef logic [BODY_BITS-1:0] body_t;
    typedef logic [LENGTH_BITS-1:0] frame_length_t;

    // Frame body starts with the Soup packet type in bits 7:0. Length counts
    // the type and the payload. The two TCP-stream prefix bytes are external.
    typedef struct packed {
        frame_length_t length;
        body_t body;
    } packet_t;

    typedef enum logic [2:0] {
        CMD_ENTER = 0,
        CMD_REPLACE = 1,
        CMD_CANCEL = 2,
        CMD_RETRY = 3,
        CMD_QUERY = 4
    } command_kind_t;

    typedef struct packed {
        logic [2:0] kind;
        logic [7:0] slot;
        logic [15:0] tag;
        logic [31:0] order_ref;
        logic [31:0] previous_order_ref;
        logic [31:0] quantity;
        logic [63:0] price;
        logic [63:0] symbol;
        logic [7:0] side;
        logic [7:0] tif;
        logic [7:0] display;
        logic [111:0] client_id;
    } command_t;

    typedef struct packed {
        logic [47:0] username;
        logic [79:0] password;
        logic [79:0] session;
        logic [63:0] sequence_start;
        logic [39:0] heartbeat_ms;
    } login_t;

    typedef enum logic [4:0] {
        N_COMMAND = 1,
        N_LOGIN = 2,
        N_LOGIN_REJECT = 3,
        N_REPLAY = 4,
        N_END = 5,
        N_FAULT = 6,
        N_ACCEPT = 7,
        N_REPLACE = 8,
        N_CANCEL = 9,
        N_EXECUTE = 10,
        N_REJECT = 11,
        N_CANCEL_PENDING = 12,
        N_CANCEL_REJECT = 13,
        N_SYSTEM = 14
    } notice_kind_t;

    typedef enum logic [7:0] {
        ST_OK = 0,
        ST_BAD_KEEP = 1,
        ST_FRAME_LENGTH = 2,
        ST_SOUP_TYPE = 3,
        ST_OUCH_TYPE = 4,
        ST_OUCH_LENGTH = 5,
        ST_OUCH_APPENDAGE = 6,
        ST_LOGIN_TEXT = 7,
        ST_LOGIN_PHASE = 8,
        ST_SESSION_ID = 9,
        ST_SEQUENCE_GAP = 10,
        ST_SEQUENCE_OVERFLOW = 11,
        ST_NOT_ACTIVE = 12,
        ST_UNSUPPORTED_CMD = 13,
        ST_MARKET_CLOSED = 14,
        ST_BAD_SLOT = 15,
        ST_SLOT_BUSY = 16,
        ST_NO_ORDER = 17,
        ST_STALE_REFERENCE = 18,
        ST_BAD_FIELDS = 19,
        ST_NO_RETRY = 20,
        ST_UNKNOWN_REFERENCE = 21,
        ST_LIFECYCLE = 22,
        ST_ACK_ECHO = 23,
        ST_BAD_DELTA = 24,
        ST_DUP_MATCH = 25,
        ST_RX_TIMEOUT = 26,
        ST_LOGIN_REJECTED = 27,
        ST_KILLED = 28,
        ST_REPLAY_WINDOW = 29,
        ST_REPLAY_DIVERGED = 30,
        ST_REPLAY_FENCE = 31
    } status_t;

    typedef struct packed {
        notice_kind_t kind;
        status_t status;
        logic [7:0] slot;
        logic [15:0] tag;
        logic [63:0] sequence_id;
        logic [31:0] order_ref;
        logic [31:0] previous_order_ref;
        logic [31:0] quantity;
        logic [31:0] leaves;
        logic [31:0] filled;
        logic [63:0] price;
        logic [63:0] timestamp;
        logic [63:0] venue_id;
        logic [15:0] reason;
    } notice_t;

    typedef enum logic [2:0] {
        S_OFFLINE,
        S_LOGIN_BUILD,
        S_LOGIN_SEND,
        S_LOGIN_WAIT,
        S_LOGIN_PARSE,
        S_ACTIVE,
        S_ENDED,
        S_FAULT
    } session_state_t;

    typedef enum logic [1:0] {
        P_NONE,
        P_ENTER,
        P_REPLACE,
        P_CANCEL
    } pending_kind_t;

    typedef struct packed {
        logic live;
        pending_kind_t pending;
        logic [31:0] order_ref;
        logic [31:0] next_order_ref;
        logic [31:0] leaves;
        logic [31:0] filled;
        logic [63:0] limit_price;
        logic [63:0] symbol;
        logic [63:0] venue_ref;
        logic [63:0] last_match;
        logic [111:0] client_id;
        logic [7:0] side;
        logic [15:0] tag;
    } slot_view_t;

    typedef struct packed {
        slot_view_t view;
        command_t pending_command;
        logic match_seen;
    } order_state_t;

    typedef struct packed {
        logic [31:0] socket_frames;
        logic [31:0] decoded_events;
        logic [31:0] replayed;
        logic [31:0] sent_commands;
        logic [31:0] retries;
        logic [31:0] local_rejects;
        logic [31:0] fault_count;
        logic [31:0] peer_heartbeats;
        logic [31:0] client_heartbeats;
    } metrics_t;

    typedef struct packed {
        logic [7:0] soup_type;
        logic [7:0] ouch_type;
        status_t status;
        logic [79:0] session;
        logic [159:0] ascii_sequence;
        logic [63:0] timestamp;
        logic [31:0] order_ref;
        logic [31:0] previous_order_ref;
        logic [31:0] quantity;
        logic [63:0] price;
        logic [63:0] symbol;
        logic [111:0] client_id;
        logic [63:0] venue_id;
        logic [15:0] reason;
        logic [7:0] side;
        logic [7:0] order_state;
    } inbound_t;

    // Reverse all 32 bits by bytes between host and wire order.
    function automatic logic [31:0] swap32(input logic [31:0] value);
        swap32 = {value[7:0], value[15:8], value[23:16], value[31:24]};
    endfunction

    // Reverse all 48 bits by bytes between host and wire order.
    function automatic logic [47:0] swap48(input logic [47:0] value);
        integer i;
        for (i = 0; i < 6; i = i + 1)
            swap48[i*8 +: 8] = value[(5-i)*8 +: 8];
    endfunction

    // Reverse all 64 bits by bytes between host and wire order.
    function automatic logic [63:0] swap64(input logic [63:0] value);
        integer i;
        for (i = 0; i < 8; i = i + 1)
            swap64[i*8 +: 8] = value[(7-i)*8 +: 8];
    endfunction

    // Reverse all 80 bits by bytes between host and wire order.
    function automatic logic [79:0] swap80(input logic [79:0] value);
        integer i;
        for (i = 0; i < 10; i = i + 1)
            swap80[i*8 +: 8] = value[(9-i)*8 +: 8];
    endfunction

    // Reverse all 112 bits by bytes between host and wire order.
    function automatic logic [111:0] swap112(input logic [111:0] value);
        integer i;
        for (i = 0; i < 14; i = i + 1)
            swap112[i*8 +: 8] = value[(13-i)*8 +: 8];
    endfunction

    // Reverse all 160 bits by bytes between host and wire order.
    function automatic logic [159:0] swap160(input logic [159:0] value);
        integer i;
        for (i = 0; i < 20; i = i + 1)
            swap160[i*8 +: 8] = value[(19-i)*8 +: 8];
    endfunction

    // Recognize the side codes supported by the OUCH enter path.
    function automatic logic valid_side(input logic [7:0] side);
        valid_side = side == "B" || side == "S";
    endfunction

    // Recognize one ASCII letter or digit without locale-dependent classification.
    function automatic logic alnum(input logic [7:0] c);
        alnum = (c >= "A" && c <= "Z") ||
                (c >= "a" && c <= "z") ||
                (c >= "0" && c <= "9");
    endfunction

    // Increment a diagnostic counter and saturate at the unsigned maximum.
    function automatic logic [31:0] incr_counter(input logic [31:0] value);
        incr_counter = (&value) ? value : value + 1'b1;
    endfunction

    // Require an alphanumeric symbol followed only by optional space padding.
    function automatic logic valid_symbol(input logic [63:0] symbol);
        logic pad_started;
        logic good;
        logic [7:0] c;
        integer pos;
        begin
            pad_started = 1'b0;
            good = symbol[63:56] != " ";
            for (pos = 7; pos >= 0; pos = pos - 1) begin
                c = symbol[pos*8 +: 8];
                if (c == " ")
                    pad_started = 1'b1;
                else if (pad_started || !((c >= "A" && c <= "Z") ||
                                          (c >= "0" && c <= "9") || c == "."))
                    good = 1'b0;
            end
            valid_symbol = good;
        end
    endfunction

    // Check the fixed-width client field for allowed characters and trailing spaces.
    function automatic logic valid_client(input logic [111:0] id);
        logic good;
        logic nonblank;
        logic [7:0] c;
        integer pos;
        begin
            good = 1'b1;
            nonblank = 1'b0;
            for (pos = 13; pos >= 0; pos = pos - 1) begin
                c = id[pos*8 +: 8];
                if (c != " ") begin
                    nonblank = 1'b1;
                    if (!alnum(c)) good = 1'b0;
                end
            end
            valid_client = good && nonblank;
        end
    endfunction

    // Check the ten-byte session name and its optional blank login form.
    function automatic logic valid_session(input logic [79:0] id, input logic allow_blank);
        logic nonblank;
        logic good;
        logic [7:0] c;
        integer pos;
        begin
            nonblank = 1'b0;
            good = 1'b1;
            for (pos = 9; pos >= 0; pos = pos - 1) begin
                c = id[pos*8 +: 8];
                if (c == " ") begin
                    if (nonblank) good = 1'b0;
                end else begin
                    nonblank = 1'b1;
                    if (!alnum(c)) good = 1'b0;
                end
            end
            valid_session = good && (nonblank || allow_blank);
        end
    endfunction

    // Check the credential field's printable non-space prefix and padding.
    function automatic logic valid_credentials(input logic [79:0] field, input integer bytes);
        logic nonblank;
        logic pad_started;
        logic good;
        logic [7:0] c;
        integer pos;
        begin
            nonblank = 1'b0;
            pad_started = 1'b0;
            good = 1'b1;
            for (pos = bytes-1; pos >= 0; pos = pos - 1) begin
                c = field[pos*8 +: 8];
                if (c == " ")
                    pad_started = 1'b1;
                else begin
                    if (pad_started || !alnum(c)) good = 1'b0;
                    nonblank = 1'b1;
                end
            end
            valid_credentials = nonblank && good;
        end
    endfunction

    // Recognize the supported ASCII decimal timeout field.
    function automatic logic valid_timeout(input logic [39:0] field);
        logic good;
        logic nonzero;
        logic [7:0] c;
        integer pos;
        begin
            good = 1'b1;
            nonzero = 1'b0;
            for (pos = 4; pos >= 0; pos = pos - 1) begin
                c = field[pos*8 +: 8];
                if (c < "0" || c > "9") good = 1'b0;
                if (c != "0") nonzero = 1'b1;
            end
            valid_timeout = good && nonzero;
        end
    endfunction

    // Encode a one-byte SoupBinTCP control packet.
    function automatic packet_t control_packet(input logic [7:0] packet_type);
        packet_t result;
        body_t bytes;
        begin
            bytes = '0;
            result.length = 1;
            bytes[0 +: 8] = packet_type;
            result.body = bytes;
            control_packet = result;
        end
    endfunction

    // Lay out the fixed-width SoupBinTCP login request in wire byte order.
    function automatic packet_t login_packet(
        input logic [47:0] username,
        input logic [79:0] password,
        input logic [79:0] session_id,
        input logic [159:0] sequence_ascii,
        input logic [39:0] timeout_ms
    );
        packet_t result;
        body_t bytes;
        begin
            bytes = '0;
            result.length = 52;
            bytes[0 +: 8] = "L";
            bytes[1*8 +: 48] = swap48(username);
            bytes[7*8 +: 80] = swap80(password);
            bytes[17*8 +: 80] = swap80(session_id);
            bytes[27*8 +: 160] = swap160(sequence_ascii);
            bytes[47*8 +: 40] = {timeout_ms[7:0], timeout_ms[15:8],
                                        timeout_ms[23:16], timeout_ms[31:24], timeout_ms[39:32]};
            result.body = bytes;
            login_packet = result;
        end
    endfunction

    // Encode a validated order entry command as a fixed OUCH wire payload.
    function automatic packet_t enter_packet(
        input logic [31:0] order_ref, input logic [7:0] side,
        input logic [31:0] quantity, input logic [63:0] symbol,
        input logic [63:0] price, input logic [7:0] tif,
        input logic [7:0] display, input logic [111:0] client_id
    );
        packet_t result;
        body_t bytes;
        begin
            bytes = '0;
            result.length = 48;
            bytes[0 +: 16] = {"O", "U"};
            bytes[2*8 +: 32] = swap32(order_ref);
            bytes[6*8 +: 8] = side;
            bytes[7*8 +: 32] = swap32(quantity);
            bytes[11*8 +: 64] = swap64(symbol);
            bytes[19*8 +: 64] = swap64(price);
            bytes[27*8 +: 8] = tif;
            bytes[28*8 +: 8] = display;
            bytes[29*8 +: 24] = {"N", "N", "P"};
            bytes[32*8 +: 112] = swap112(client_id);
            result.body = bytes;
            enter_packet = result;
        end
    endfunction

    // Encode both old and new references plus replace attributes in wire order.
    function automatic packet_t replace_packet(
        input logic [31:0] previous_order_ref, input logic [31:0] order_ref,
        input logic [31:0] quantity, input logic [63:0] price,
        input logic [7:0] tif, input logic [7:0] display,
        input logic [111:0] client_id
    );
        packet_t result;
        body_t bytes;
        begin
            bytes = '0;
            result.length = 41;
            bytes[0 +: 16] = {"U", "U"};
            bytes[2*8 +: 32] = swap32(previous_order_ref);
            bytes[6*8 +: 32] = swap32(order_ref);
            bytes[10*8 +: 32] = swap32(quantity);
            bytes[14*8 +: 64] = swap64(price);
            bytes[22*8 +: 8] = tif;
            bytes[23*8 +: 8] = display;
            bytes[24*8 +: 8] = "N";
            bytes[25*8 +: 112] = swap112(client_id);
            result.body = bytes;
            replace_packet = result;
        end
    endfunction

    // Encode the reference and cancel quantity as an OUCH request.
    function automatic packet_t cancel_packet(
        input logic [31:0] order_ref, input logic [31:0] quantity
    );
        packet_t result;
        body_t bytes;
        begin
            bytes = '0;
            result.length = 10;
            bytes[0 +: 16] = {"X", "U"};
            bytes[2*8 +: 32] = swap32(order_ref);
            bytes[6*8 +: 32] = swap32(quantity);
            result.body = bytes;
            cancel_packet = result;
        end
    endfunction

    // Select the wire encoder for enter, replace or cancel.
    function automatic packet_t encode_command(
        input logic [2:0] kind, input logic [31:0] previous_order_ref,
        input logic [31:0] order_ref, input logic [31:0] quantity,
        input logic [63:0] price, input logic [63:0] symbol, input logic [7:0] side,
        input logic [7:0] tif, input logic [7:0] display, input logic [111:0] client_id
    );
        case (kind)
            CMD_ENTER: encode_command = enter_packet(order_ref, side, quantity, symbol,
                                                      price, tif, display, client_id);
            CMD_REPLACE: encode_command = replace_packet(previous_order_ref, order_ref, quantity,
                                                          price, tif, display, client_id);
            CMD_CANCEL: encode_command = cancel_packet(previous_order_ref, quantity);
            default: encode_command = '0;
        endcase
    endfunction
endpackage

`default_nettype wire
