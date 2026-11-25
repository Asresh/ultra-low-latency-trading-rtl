`timescale 1ns/1ps
`default_nettype none

// Structural checks for one staged OCP1 row. A separate scan checks duplicate
// references before any state is applied to the native engine.
module ouch_checkpoint_validate (
    input wire [7:0] slot,
    input wire [31:0] highest_ref,
    input ouch_wire_pkg::order_state_t image,
    output logic valid
);
    import ouch_wire_pkg::*;
    slot_view_t view_data;
    command_t pending;
    logic fields_ok, displayed, tif_ok, price_ok;

    always @* begin
        view_data = image.view;
        pending = image.pending_command;
        displayed = pending.display == "Y" || pending.display == "N";
        tif_ok = pending.tif == "0" || pending.tif == "3" || pending.tif == "5";
        price_ok = pending.price != 0 && pending.price <= MAX_LIMIT_PRICE;
        fields_ok = pending.slot == slot && pending.quantity != 0 && pending.quantity <= MAX_SHARES &&
                    displayed && tif_ok && price_ok && valid_client(pending.client_id);

        if (view_data.order_ref == 0)
            valid = image == '0;
        else begin
            // A retired row retains its identity and pricing, but no leaves
            // or unresolved commands. Any allocated ref is below high-water.
            valid = view_data.order_ref <= highest_ref && view_data.leaves <= MAX_SHARES &&
                    33'(view_data.leaves) + 33'(view_data.filled) <= 33'(MAX_SHARES) &&
                    view_data.live == (view_data.leaves != 0) &&
                    view_data.limit_price != 0 && view_data.limit_price <= MAX_LIMIT_PRICE &&
                    valid_symbol(view_data.symbol) && valid_side(view_data.side) &&
                    valid_client(view_data.client_id) && (image.match_seen || view_data.last_match == 0);
            case (view_data.pending)
                P_NONE: valid &= pending == '0 && view_data.next_order_ref == 0;
                P_ENTER: valid &= !view_data.live && !image.match_seen && view_data.next_order_ref == 0 &&
                    view_data.filled == 0 && view_data.venue_ref == 0 && pending.kind == CMD_ENTER &&
                    fields_ok && pending.previous_order_ref == 0 && pending.order_ref == view_data.order_ref &&
                    pending.symbol == view_data.symbol && pending.side == view_data.side &&
                    pending.client_id == view_data.client_id && pending.price == view_data.limit_price &&
                    pending.tag == view_data.tag;
                P_REPLACE: valid &= view_data.live && pending.kind == CMD_REPLACE && fields_ok &&
                    pending.previous_order_ref == view_data.order_ref &&
                    pending.order_ref == view_data.next_order_ref && pending.order_ref > view_data.order_ref &&
                    pending.order_ref <= highest_ref;
                P_CANCEL: valid &= view_data.live && pending.kind == CMD_CANCEL && pending.slot == slot &&
                    pending.order_ref == view_data.order_ref && pending.previous_order_ref == view_data.order_ref &&
                    view_data.next_order_ref == 0 && pending.quantity <= MAX_SHARES;
                default: valid = 1'b0;
            endcase
        end
    end
endmodule

`default_nettype wire
