`timescale 1ns/1ps
`default_nettype none

module ouch_order_table_engine #(
    parameter integer ORDER_SLOTS = 8
) (
    input wire clk,
    input wire rst,
    input ouch_wire_pkg::command_t command,
    input ouch_wire_pkg::status_t cmd_session_status,
    input ouch_wire_pkg::status_t admission_status,
    input wire cmd_commit,
    output ouch_wire_pkg::notice_t cmd_notice,
    output ouch_wire_pkg::packet_t cmd_packet,
    output wire cmd_sends,
    input ouch_wire_pkg::inbound_t inbound,
    input wire [63:0] sequence_id,
    input wire rx_commit,
    output ouch_wire_pkg::notice_t rx_notice,
    input wire [7:0] query_slot,
    output ouch_wire_pkg::slot_view_t query_view,
    output wire [31:0] highest_order_ref,
    output wire [7:0] live_count,
    output wire [7:0] pending_count,
    input wire [7:0] inspect_slot,
    output ouch_wire_pkg::order_state_t inspected,
    input wire restore_slot_valid,
    input wire [7:0] restore_slot,
    input ouch_wire_pkg::order_state_t restore_state,
    input wire restore_ref_valid,
    input wire [31:0] restore_high_water
);
    import ouch_wire_pkg::*;
    localparam integer ORDER_BITS = $bits(order_state_t);
    localparam integer INDEX_W = ORDER_SLOTS > 1 ? $clog2(ORDER_SLOTS) : 1;
    localparam logic [8:0] CAPACITY = 9'(ORDER_SLOTS);
    typedef logic [INDEX_W-1:0] index_t;

    logic [ORDER_BITS-1:0] data_by_slot [0:ORDER_SLOTS-1];
    logic [31:0] high_water;
    logic cmd_write, cmd_new_ref;
    logic rx_write;
    logic [7:0] match_slot, live_total, pending_total;
    logic [31:0] rx_key;
    logic match_found;
    logic match_conflict;
    order_state_t chosen_cmd, chosen_rx, command_after, inbound_after, query_entry;
    order_state_t indexed_entry;
    notice_t cmd_result, rx_result;
    packet_t tx_result;
    command_t tx_replay;
    slot_view_t cmd_view_after, rx_view_after;
    wire cmd_address_ok = {1'b0, command.slot} < CAPACITY;
    wire query_address_ok = {1'b0, query_slot} < CAPACITY;

    assign cmd_notice = cmd_result;
    assign cmd_packet = tx_result;
    assign cmd_sends = tx_result.length != 0;
    assign rx_notice = rx_result;
    assign highest_order_ref = high_water;
    assign live_count = live_total;
    assign pending_count = pending_total;
    assign query_view = query_entry.view;
    assign inspected = {1'b0,inspect_slot} < CAPACITY ? data_by_slot[index_t'(inspect_slot)] : '0;

    // Check that the time-in-force code is supported by this gateway subset.
    function automatic logic tif_allowed(input logic [7:0] tif);
        tif_allowed = tif == "0" || tif == "3" || tif == "5";
    endfunction

    // Check that the requested display mode is supported.
    function automatic logic display_allowed(input logic [7:0] display);
        display_allowed = display == "Y" || display == "N";
    endfunction

    // Validate the wire-format limit price before comparing or storing quantities.
    function automatic logic price_allowed(input logic [63:0] price);
        price_allowed = price != 0 && price <= MAX_LIMIT_PRICE;
    endfunction

    // Compare a price against the stored limit using the order side.
    function automatic logic better_or_equal(
        input logic [63:0] accepted, input logic [63:0] requested,
        input logic [7:0] side
    );
        better_or_equal = price_allowed(accepted) &&
                         ((side == "B" && accepted <= requested) ||
                          (side == "S" && accepted >= requested));
    endfunction

    // Check that a venue status byte agrees with the remaining quantity.
    function automatic logic valid_order_state(input logic [7:0] encoding, input logic [31:0] qty);
        valid_order_state = encoding == "D" || (encoding == "L" && qty != 0);
    endfunction

    // Remove the in-flight request fields while retaining the live order identity.
    function automatic order_state_t clear_pending(input order_state_t before_value);
        order_state_t result;
        begin
            result = before_value;
            result.view.pending = P_NONE;
            result.view.next_order_ref = 0;
            result.pending_command = '0;
            clear_pending = result;
        end
    endfunction

    // Clear pending transaction fields and mark the live order as terminal.
    function automatic order_state_t close_order(input order_state_t before_value);
        order_state_t result;
        begin
            result = clear_pending(before_value);
            result.view.live = 1'b0;
            result.view.leaves = 0;
            close_order = result;
        end
    endfunction

    always @* begin
        // Replace responses address the old order_ref. Replace rejects address
        // the pending new order_ref. Both searches are limited to eligible slots.
        rx_key = inbound.ouch_type == "U" ? inbound.previous_order_ref : inbound.order_ref;
        chosen_cmd = '0;
        chosen_rx = '0;
        query_entry = '0;
        match_slot = NO_SLOT;
        match_found = 1'b0;
        match_conflict = 1'b0;
        live_total = 0;
        pending_total = 0;
        indexed_entry = '0;
        for (integer i = 0; i < ORDER_SLOTS; i = i + 1) begin
            indexed_entry = data_by_slot[i];
            if (indexed_entry.view.live)
                live_total = live_total + 1'b1;
            if (indexed_entry.view.pending != P_NONE)
                pending_total = pending_total + 1'b1;
            if (command.slot == 8'(i))
                chosen_cmd = indexed_entry;
            if (query_slot == 8'(i))
                query_entry = indexed_entry;
            if (rx_key != 0 &&
                (((indexed_entry.view.live || indexed_entry.view.pending == P_ENTER) &&
                  indexed_entry.view.order_ref == rx_key) ||
                 (inbound.ouch_type == "J" && indexed_entry.view.pending == P_REPLACE &&
                  indexed_entry.view.next_order_ref == rx_key))) begin
                if (match_found)
                    match_conflict = 1'b1;
                match_found = 1'b1;
                match_slot = 8'(i);
                chosen_rx = indexed_entry;
            end
        end
        if (!query_address_ok) query_entry = '0;
    end

    logic [32:0] command_liability;
    logic enter_fields_ok;
    logic common_fields_ok;

    always @* begin
        command_after = chosen_cmd;
        cmd_view_after = chosen_cmd.view;
        cmd_write = 1'b0;
        cmd_new_ref = 1'b0;
        cmd_result = '0;
        cmd_result.kind = N_COMMAND;
        cmd_result.slot = command.slot;
        cmd_result.tag = command.tag;
        cmd_result.order_ref = command.order_ref;
        cmd_result.previous_order_ref = command.previous_order_ref;
        cmd_result.status = cmd_session_status;
        cmd_result.quantity = command.quantity;
        cmd_result.price = command.price;
        tx_result = '0;
        tx_replay = chosen_cmd.pending_command;
        command_liability = {1'b0, chosen_cmd.view.leaves} + {1'b0, chosen_cmd.view.filled};
        common_fields_ok = price_allowed(command.price) && command.quantity != 0 &&
                           command.quantity <= MAX_SHARES && valid_client(command.client_id) &&
                           tif_allowed(command.tif) && display_allowed(command.display);
        enter_fields_ok = common_fields_ok && valid_symbol(command.symbol) &&
                          valid_side(command.side) && command.previous_order_ref == 0;

        if (cmd_result.status == ST_OK && !cmd_address_ok) begin
            cmd_result.status = ST_BAD_SLOT;
        end else if (cmd_result.status == ST_OK) begin
            case (command.kind)
                CMD_ENTER: begin
                    if (chosen_cmd.view.live || chosen_cmd.view.pending != P_NONE) begin
                        cmd_result.status = ST_SLOT_BUSY;
                    end else if (command.order_ref <= high_water) begin
                        cmd_result.status = ST_STALE_REFERENCE;
                    end else if (!enter_fields_ok) begin
                        cmd_result.status = ST_BAD_FIELDS;
                    end else begin
                        command_after = '0;
                        command_after.view.pending = P_ENTER;
                        command_after.view.order_ref = command.order_ref;
                        command_after.view.limit_price = command.price;
                        command_after.view.symbol = command.symbol;
                        command_after.view.client_id = command.client_id;
                        command_after.view.side = command.side;
                        command_after.view.tag = command.tag;
                        command_after.pending_command = command;
                        cmd_write = 1'b1;
                        cmd_new_ref = 1'b1;
                    end
                end
                CMD_REPLACE: begin
                    if (!chosen_cmd.view.live) begin
                        cmd_result.status = ST_NO_ORDER;
                    end else if (chosen_cmd.view.pending != P_NONE) begin
                        cmd_result.status = ST_SLOT_BUSY;
                    end else if (command.order_ref <= high_water ||
                                 command.previous_order_ref != chosen_cmd.view.order_ref) begin
                        cmd_result.status = ST_STALE_REFERENCE;
                    end else if (!common_fields_ok || command.quantity <= chosen_cmd.view.filled) begin
                        cmd_result.status = ST_BAD_FIELDS;
                    end else begin
                        command_after.view.pending = P_REPLACE;
                        command_after.view.next_order_ref = command.order_ref;
                        command_after.pending_command = command;
                        cmd_write = 1'b1;
                        cmd_new_ref = 1'b1;
                    end
                end
                CMD_CANCEL: begin
                    if (!chosen_cmd.view.live) begin
                        cmd_result.status = ST_NO_ORDER;
                    end else if (chosen_cmd.view.pending != P_NONE) begin
                        cmd_result.status = ST_SLOT_BUSY;
                    end else if (command.order_ref != chosen_cmd.view.order_ref) begin
                        cmd_result.status = ST_STALE_REFERENCE;
                    end else if (command.quantity != 0 &&
                                 (command.quantity < chosen_cmd.view.filled ||
                                  {1'b0, command.quantity} >= command_liability)) begin
                        cmd_result.status = ST_BAD_FIELDS;
                    end else begin
                        command_after.view.pending = P_CANCEL;
                        command_after.pending_command = command;
                        command_after.pending_command.previous_order_ref = command.order_ref;
                        cmd_write = 1'b1;
                    end
                end
                CMD_RETRY: begin
                    if (chosen_cmd.view.pending == P_NONE) begin
                        cmd_result.status = ST_NO_RETRY;
                    end else if (command.order_ref != tx_replay.order_ref) begin
                        cmd_result.status = ST_STALE_REFERENCE;
                    end else if (chosen_cmd.view.pending != P_CANCEL && admission_status != ST_OK) begin
                        cmd_result.status = admission_status;
                    end
                end
                CMD_QUERY: begin
                    cmd_result.order_ref = chosen_cmd.view.order_ref;
                    cmd_result.previous_order_ref = chosen_cmd.view.next_order_ref;
                    cmd_result.quantity = chosen_cmd.pending_command.quantity;
                    cmd_result.price = chosen_cmd.view.limit_price;
                    cmd_result.reason = {13'b0, chosen_cmd.view.live, chosen_cmd.view.pending};
                end
                default: cmd_result.status = ST_UNSUPPORTED_CMD;
            endcase
        end
        if (cmd_result.status == ST_OK && (cmd_write || command.kind == CMD_RETRY)) begin
            if (command.kind != CMD_RETRY) tx_replay = command_after.pending_command;
            tx_result = encode_command(
                tx_replay.kind, tx_replay.previous_order_ref, tx_replay.order_ref, tx_replay.quantity,
                tx_replay.price, tx_replay.symbol, tx_replay.side, tx_replay.tif,
                tx_replay.display, tx_replay.client_id
            );
        end
        if (cmd_address_ok) begin
            cmd_view_after = command_after.view;
            cmd_result.leaves = cmd_view_after.leaves;
            cmd_result.filled = cmd_view_after.filled;
            cmd_result.venue_id = cmd_view_after.venue_ref;
        end
    end

    logic rx_is_live;
    logic echo_ok;
    logic [32:0] chain_qty;
    logic [31:0] leaves_after_delta;

    always @* begin
        inbound_after = chosen_rx;
        rx_view_after = chosen_rx.view;
        rx_write = 1'b0;
        rx_result = '0;
        rx_result.slot = match_slot;
        rx_result.status = ST_OK;
        rx_result.sequence_id = sequence_id;
        rx_result.order_ref = inbound.order_ref;
        rx_result.previous_order_ref = inbound.previous_order_ref;
        rx_result.quantity = inbound.quantity;
        rx_result.price = inbound.price;
        rx_result.timestamp = inbound.timestamp;
        rx_result.venue_id = inbound.venue_id;
        rx_result.reason = inbound.reason;
        rx_result.tag = chosen_rx.view.tag;
        rx_result.kind = N_FAULT;
        rx_is_live = inbound.order_state == "L";
        chain_qty = {1'b0, chosen_rx.view.filled} + {1'b0, inbound.quantity};
        leaves_after_delta = chosen_rx.view.leaves - inbound.quantity;
        echo_ok = inbound.side == chosen_rx.view.side && inbound.symbol == chosen_rx.view.symbol &&
                  inbound.client_id == chosen_rx.pending_command.client_id &&
                  better_or_equal(inbound.price, chosen_rx.pending_command.price, inbound.side) &&
                  valid_order_state(inbound.order_state, inbound.quantity);

        if (!match_found || match_conflict) begin
            if (match_found) rx_result.status = ST_LIFECYCLE;
            else rx_result.status = ST_UNKNOWN_REFERENCE;
        end else begin
            case (inbound.ouch_type)
                "A": begin
                    rx_result.kind = N_ACCEPT;
                    if (chosen_rx.view.pending != P_ENTER) begin
                        rx_result.status = ST_LIFECYCLE;
                    end else if (!echo_ok || inbound.quantity > chosen_rx.pending_command.quantity) begin
                        rx_result.status = ST_ACK_ECHO;
                    end else begin
                        inbound_after = clear_pending(chosen_rx);
                        inbound_after.view.live = rx_is_live;
                        inbound_after.view.limit_price = inbound.price;
                        inbound_after.view.venue_ref = inbound.venue_id;
                        inbound_after.view.leaves = rx_is_live ? inbound.quantity : 0;
                        rx_write = 1'b1;
                    end
                end
                "U": begin
                    rx_result.kind = N_REPLACE;
                    rx_result.tag = chosen_rx.pending_command.tag;
                    if (chosen_rx.view.pending != P_REPLACE ||
                        inbound.order_ref != chosen_rx.view.next_order_ref) begin
                        rx_result.status = ST_LIFECYCLE;
                    end else if (!echo_ok || chain_qty > {1'b0, chosen_rx.pending_command.quantity}) begin
                        rx_result.status = ST_ACK_ECHO;
                    end else begin
                        inbound_after = clear_pending(chosen_rx);
                        inbound_after.view.live = rx_is_live;
                        inbound_after.view.order_ref = inbound.order_ref;
                        inbound_after.view.limit_price = inbound.price;
                        inbound_after.view.venue_ref = inbound.venue_id;
                        inbound_after.view.leaves = rx_is_live ? inbound.quantity : 0;
                        inbound_after.view.client_id = inbound.client_id;
                        inbound_after.view.tag = rx_result.tag;
                        rx_write = 1'b1;
                    end
                end
                "C", "E": begin
                    if (inbound.ouch_type == "E") rx_result.kind = N_EXECUTE;
                    else rx_result.kind = N_CANCEL;
                    if (!chosen_rx.view.live) begin
                        rx_result.status = ST_LIFECYCLE;
                    end else if (inbound.quantity == 0 || inbound.quantity > chosen_rx.view.leaves ||
                                 (inbound.ouch_type == "E" && !price_allowed(inbound.price))) begin
                        rx_result.status = ST_BAD_DELTA;
                    end else if (inbound.ouch_type == "E" && chosen_rx.match_seen &&
                                 inbound.venue_id == chosen_rx.view.last_match) begin
                        rx_result.status = ST_DUP_MATCH;
                    end else begin
                        if (leaves_after_delta == 0)
                            inbound_after = close_order(chosen_rx);
                        else if (inbound.ouch_type == "C" && chosen_rx.view.pending == P_CANCEL)
                            inbound_after = clear_pending(chosen_rx);
                        inbound_after.view.leaves = leaves_after_delta;
                        if (inbound.ouch_type == "E") begin
                            inbound_after.view.filled = chain_qty[31:0];
                            inbound_after.view.last_match = inbound.venue_id;
                            inbound_after.match_seen = 1'b1;
                        end
                        rx_write = 1'b1;
                    end
                end
                "J": begin
                    rx_result.kind = N_REJECT;
                    rx_result.tag = chosen_rx.pending_command.tag;
                    if (chosen_rx.view.pending != P_ENTER && chosen_rx.view.pending != P_REPLACE) begin
                        rx_result.status = ST_LIFECYCLE;
                    end else if (inbound.client_id != chosen_rx.pending_command.client_id ||
                                 inbound.order_ref != chosen_rx.pending_command.order_ref) begin
                        rx_result.status = ST_ACK_ECHO;
                    end else begin
                        if (chosen_rx.view.pending == P_ENTER)
                            inbound_after = close_order(chosen_rx);
                        else
                            inbound_after = clear_pending(chosen_rx);
                        rx_write = 1'b1;
                    end
                end
                "P", "I": begin
                    if (inbound.ouch_type == "P") rx_result.kind = N_CANCEL_PENDING;
                    else rx_result.kind = N_CANCEL_REJECT;
                    rx_result.tag = chosen_rx.pending_command.tag;
                    if (!chosen_rx.view.live || chosen_rx.view.pending != P_CANCEL) begin
                        rx_result.status = ST_LIFECYCLE;
                    end else if (inbound.ouch_type == "I") begin
                        inbound_after = clear_pending(chosen_rx);
                        rx_write = 1'b1;
                    end
                end
                default: rx_result.status = ST_OUCH_TYPE;
            endcase
        end
        if (match_found) begin
            rx_view_after = inbound_after.view;
            rx_result.leaves = rx_view_after.leaves;
            rx_result.filled = rx_view_after.filled;
        end
    end

    integer i;
    always @(posedge clk) begin
        if (rst) begin
            high_water <= 0;
            for (i = 0; i < ORDER_SLOTS; i = i + 1)
                data_by_slot[i] <= '0;
        end else if (restore_ref_valid) begin
            high_water <= restore_high_water;
        end else if (restore_slot_valid) begin
            data_by_slot[index_t'(restore_slot)] <= restore_state;
        end else if (rx_commit && rx_write && rx_result.status == ST_OK) begin
            data_by_slot[index_t'(match_slot)] <= inbound_after;
        end else if (cmd_commit && cmd_write && cmd_result.status == ST_OK) begin
            data_by_slot[index_t'(command.slot)] <= command_after;
            if (cmd_new_ref) high_water <= command.order_ref;
        end
    end
endmodule

`default_nettype wire
