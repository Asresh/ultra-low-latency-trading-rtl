`timescale 1ns/1ps
`default_nettype none

// A VLAN-scoped forwarding table for selecting the next-hop address passed to
// the ARP endpoint. Configuration has a shadow bank and atomic activation.
module eth_ipv4_route #(
    parameter integer ROWS = 8,
    parameter integer COOKIE_W = 16,
    parameter integer EPOCH_W = 16,
    localparam integer ROW_W = ROWS <= 1 ? 1 : $clog2(ROWS+1)
) (
    input wire clk,
    input wire rst,
    input wire s_valid,
    output wire s_ready,
    input wire s_tagged,
    input wire [11:0] s_vid,
    input wire [31:0] s_destination,
    input wire [COOKIE_W-1:0] s_cookie,

    output logic m_valid,
    input wire m_ready,
    output logic [1:0] m_status,
    output logic [COOKIE_W-1:0] m_cookie,
    output logic [EPOCH_W-1:0] m_epoch,
    output logic m_tagged,
    output logic [11:0] m_vid,
    output logic [31:0] m_next_hop,
    output logic [ROW_W-1:0] m_row,

    input wire cfg_valid,
    output wire cfg_ready,
    input wire [ROW_W-1:0] cfg_row,
    input wire cfg_enabled,
    input wire cfg_tagged,
    input wire [11:0] cfg_vid,
    input wire [31:0] cfg_network,
    input wire [5:0] cfg_prefix,
    input wire cfg_reject,
    input wire [31:0] cfg_gateway,
    output logic cfg_error,
    input wire commit_valid,
    output wire commit_ready,
    output logic [EPOCH_W-1:0] active_epoch
);
    localparam logic [1:0] FOUND = 0, MISS = 1, DENIED = 2, BAD_DEST = 3;
    localparam integer INDEX_W = ROWS <= 1 ? 1 : $clog2(ROWS);
    logic active_bank;
    logic [ROWS-1:0] enabled [0:1];
    logic [12:0] vlan_key [0:1][0:ROWS-1];
    logic [31:0] networks [0:1][0:ROWS-1];
    logic [31:0] masks [0:1][0:ROWS-1];
    logic [5:0] prefixes [0:1][0:ROWS-1];
    logic [31:0] gateways [0:1][0:ROWS-1];
    logic rejects [0:1][0:ROWS-1];
    logic found;
    logic [INDEX_W-1:0] selected;
    logic [5:0] selected_length;
    wire shadow = !active_bank;
    wire [11:0] requested_vid = s_tagged ? s_vid : 12'd0;
    wire [12:0] request_vlan = {s_tagged,requested_vid};
    wire [31:0] config_mask = mask_for_prefix(cfg_prefix);
    wire [INDEX_W-1:0] write_at = ROWS == 1 ? '0 : INDEX_W'(cfg_row);
    wire legal_gateway = cfg_gateway == 0 || usable_address(cfg_gateway[31:24]);
    wire route_fields_ok = !cfg_enabled || (
        int'(cfg_prefix) <= 32 && (cfg_network & ~config_mask) == 0 &&
        (!cfg_tagged || cfg_vid != 12'hfff) && legal_gateway &&
        (!cfg_reject || cfg_gateway == 0)
    );

    function automatic logic [31:0] mask_for_prefix(input logic [5:0] bits);
        mask_for_prefix = (bits == 0) ? 32'd0 : (32'hffff_ffff << (32-int'(bits)));
    endfunction

    function automatic logic usable_address(input logic [7:0] first_octet);
        usable_address = first_octet != 0 && first_octet != 8'd127 && first_octet < 8'd224;
    endfunction

    assign s_ready = !rst && (!m_valid || m_ready);
    assign commit_ready = !rst;
    assign cfg_ready = !rst && !commit_valid;

    // Compare length only after a hit; walk low-to-high to keep the first tie.
    always @* begin
        found = 1'b0;
        selected = '0;
        selected_length = '0;
        for (integer row = 0; row < ROWS; row++) begin
            if (enabled[active_bank][row] && vlan_key[active_bank][row] == request_vlan &&
                (s_destination & masks[active_bank][row]) == networks[active_bank][row] &&
                (!found || prefixes[active_bank][row] > selected_length)) begin
                found = 1'b1;
                selected = INDEX_W'(row);
                selected_length = prefixes[active_bank][row];
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            enabled[0] <= '0;
            enabled[1] <= '0;
            active_bank <= 1'b0;
            active_epoch <= '0;
            cfg_error <= 1'b0;
            m_valid <= 1'b0;
            m_status <= '0;
            m_cookie <= '0;
            m_epoch <= '0;
            m_tagged <= 1'b0;
            m_vid <= '0;
            m_next_hop <= '0;
            m_row <= '0;
        end else begin
            cfg_error <= 1'b0;
            if (commit_valid && commit_ready) begin
                active_bank <= shadow;
                active_epoch <= active_epoch + 1'b1;
            end
            if (cfg_valid && cfg_ready) begin
                if (int'(cfg_row) >= ROWS || !route_fields_ok)
                    cfg_error <= 1'b1;
                else begin
                    enabled[shadow][write_at] <= cfg_enabled;
                    vlan_key[shadow][write_at] <= {cfg_tagged,cfg_tagged ? cfg_vid : 12'd0};
                    networks[shadow][write_at] <= cfg_network;
                    masks[shadow][write_at] <= config_mask;
                    prefixes[shadow][write_at] <= cfg_prefix;
                    gateways[shadow][write_at] <= cfg_gateway;
                    rejects[shadow][write_at] <= cfg_reject;
                end
            end

            if (s_ready) begin
                m_valid <= s_valid;
                if (s_valid) begin
                    m_cookie <= s_cookie;
                    m_epoch <= active_epoch;
                    m_tagged <= s_tagged;
                    m_vid <= requested_vid;
                    m_next_hop <= '0;
                    m_row <= '1;
                    if (!usable_address(s_destination[31:24]) || (s_tagged && s_vid == 12'hfff))
                        m_status <= BAD_DEST;
                    else if (!found)
                        m_status <= MISS;
                    else begin
                        m_row <= ROW_W'(selected);
                        if (rejects[active_bank][selected])
                            m_status <= DENIED;
                        else begin
                            m_status <= FOUND;
                            m_next_hop <= gateways[active_bank][selected] == 0
                                ? s_destination : gateways[active_bank][selected];
                        end
                    end
                end
            end
        end
    end
endmodule

`default_nettype wire
