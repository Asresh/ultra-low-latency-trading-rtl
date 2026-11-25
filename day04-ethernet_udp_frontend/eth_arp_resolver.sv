`timescale 1ns/1ps
`default_nettype none

// Resolve multiple VLAN-scoped IPv4 neighbors independently. The wire adapter
// validates that replies target the local endpoint before presenting them here.
module eth_arp_resolver #(
    parameter integer CACHE_DEPTH = 8,
    parameter integer PENDING_DEPTH = 4,
    parameter integer WAITERS = 4,
    parameter integer RETRY_CYCLES = 32,
    parameter integer MAX_ATTEMPTS = 3,
    parameter integer CACHE_TTL = 1024,
    parameter integer COOKIE_W = 16,
    localparam integer CACHE_W = CACHE_DEPTH <= 1 ? 1 : $clog2(CACHE_DEPTH),
    localparam integer SLOT_W = PENDING_DEPTH <= 1 ? 1 : $clog2(PENDING_DEPTH),
    localparam integer WAITER_W = WAITERS <= 1 ? 1 : $clog2(WAITERS),
    localparam integer COUNT_W = $clog2(WAITERS+1),
    localparam integer ATTEMPT_W = $clog2(MAX_ATTEMPTS+1)
) (
    input wire clk,
    input wire rst,
    input wire s_valid,
    output wire s_ready,
    input wire s_tagged,
    input wire [11:0] s_vid,
    input wire [31:0] s_ip,
    input wire [COOKIE_W-1:0] s_cookie,
    output logic m_valid,
    input wire m_ready,
    output logic [2:0] m_status,
    output logic [COOKIE_W-1:0] m_cookie,
    output logic [47:0] m_mac,

    input wire s_reply_valid,
    output wire s_reply_ready,
    input wire s_reply_tagged,
    input wire [11:0] s_reply_vid,
    input wire [31:0] s_reply_ip,
    input wire [47:0] s_reply_mac,
    output logic m_probe_valid,
    input wire m_probe_ready,
    output wire m_probe_tagged,
    output wire [11:0] m_probe_vid,
    output wire [31:0] m_probe_ip,

    output logic [31:0] cache_hits,
    output logic [31:0] misses,
    output logic [31:0] joined,
    output logic [31:0] probes_sent,
    output logic [31:0] replies_learned,
    output logic [31:0] replies_ignored,
    output logic [31:0] timeouts
);
    localparam logic [2:0] RESOLVED = 0, TIMEOUT = 1, BUSY = 2, BAD_ADDRESS = 3;
    localparam logic [1:0] FREE = 0, OPEN = 1, COMPLETE = 2;
    typedef logic [44:0] key_t;

    logic [31:0] tick;
    logic [CACHE_DEPTH-1:0] cache_valid;
    key_t cache_key [0:CACHE_DEPTH-1];
    logic [47:0] cache_mac [0:CACHE_DEPTH-1];
    logic [31:0] cache_born [0:CACHE_DEPTH-1];
    logic [CACHE_W-1:0] eviction_pointer;

    logic [1:0] pending_state [0:PENDING_DEPTH-1];
    key_t pending_key [0:PENDING_DEPTH-1];
    logic [ATTEMPT_W-1:0] pending_attempts [0:PENDING_DEPTH-1];
    logic [31:0] pending_last_probe [0:PENDING_DEPTH-1];
    logic [2:0] pending_result [0:PENDING_DEPTH-1];
    logic [47:0] pending_mac [0:PENDING_DEPTH-1];
    logic [COUNT_W-1:0] used [0:PENDING_DEPTH-1];
    logic [WAITER_W-1:0] head [0:PENDING_DEPTH-1], tail [0:PENDING_DEPTH-1];
    logic [COOKIE_W-1:0] waiters [0:PENDING_DEPTH-1][0:WAITERS-1];

    logic [SLOT_W-1:0] probe_slot, probe_cursor, drain_cursor;
    key_t probe_key;
    logic control_priority;
    wire [PENDING_DEPTH-1:0] deadlines;
    wire [CACHE_DEPTH-1:0] cache_alive;

    wire lookup_key_ok = s_ip[31:24] != 0 && s_ip[31:24] != 8'd127 &&
                         s_ip[31:28] < 4'd14 && (!s_tagged || s_vid != 12'hfff);
    wire mac_ok = s_reply_mac != 0 && !s_reply_mac[40];
    wire response_room = !m_valid || m_ready;
    wire probe_fire = m_probe_valid && m_probe_ready;
    wire can_replace_probe = !m_probe_valid || m_probe_ready;
    wire [44:0] request_key = {s_tagged, s_tagged ? s_vid : 12'd0, s_ip};
    wire [44:0] reply_key = {s_reply_tagged, s_reply_tagged ? s_reply_vid : 12'd0, s_reply_ip};

    logic cache_hit, evict_found, refresh_found;
    logic [CACHE_W-1:0] cache_hit_at, victim_at, refresh_at;
    logic join_found, free_found, learn_found, drain_found, probe_found;
    logic [SLOT_W-1:0] join_at, free_at, learn_at, drain_at, probe_at;

    function automatic logic [31:0] inc_sat(input logic [31:0] value);
        inc_sat = (&value) ? value : value + 1'b1;
    endfunction

    function automatic logic [WAITER_W-1:0] waiter_next(input logic [WAITER_W-1:0] position);
        waiter_next = position == WAITER_W'(WAITERS-1) ? '0 : position + 1'b1;
    endfunction

    function automatic logic [SLOT_W-1:0] slot_next(input logic [SLOT_W-1:0] position);
        slot_next = position == SLOT_W'(PENDING_DEPTH-1) ? '0 : position + 1'b1;
    endfunction

    for (genvar row = 0; row < CACHE_DEPTH; row = row + 1) begin : g_age
        assign cache_alive[row] = cache_valid[row] && tick - cache_born[row] < 32'(CACHE_TTL);
    end

    for (genvar row = 0; row < PENDING_DEPTH; row = row + 1) begin : g_deadline
        assign deadlines[row] = pending_state[row] == OPEN &&
                                pending_attempts[row] == ATTEMPT_W'(MAX_ATTEMPTS) &&
                                tick - pending_last_probe[row] >= 32'(RETRY_CYCLES);
    end

    // Reuse the first expired cache row before rotating through live entries.
    always @* begin
        cache_hit = 1'b0;
        evict_found = 1'b0;
        refresh_found = 1'b0;
        cache_hit_at = '0;
        victim_at = eviction_pointer;
        refresh_at = '0;
        for (integer row = 0; row < CACHE_DEPTH; row++) begin
            if (cache_alive[row] && cache_key[row] == request_key) begin
                cache_hit = 1'b1;
                cache_hit_at = CACHE_W'(row);
            end
            if (!evict_found && !cache_alive[row]) begin
                evict_found = 1'b1;
                victim_at = CACHE_W'(row);
            end
            if (cache_alive[row] && cache_key[row] == reply_key) begin
                refresh_found = 1'b1;
                refresh_at = CACHE_W'(row);
            end
        end
    end

    always @* begin
        join_found = 1'b0;
        free_found = 1'b0;
        learn_found = 1'b0;
        join_at = '0;
        free_at = '0;
        learn_at = '0;
        for (integer row = 0; row < PENDING_DEPTH; row++) begin
            if (!free_found && pending_state[row] == FREE) begin
                free_found = 1'b1;
                free_at = SLOT_W'(row);
            end
            if (pending_state[row] == OPEN && !deadlines[row]) begin
                if (pending_key[row] == request_key) begin
                    join_found = 1'b1;
                    join_at = SLOT_W'(row);
                end
                if (pending_attempts[row] != 0 && pending_key[row] == reply_key) begin
                    learn_found = 1'b1;
                    learn_at = SLOT_W'(row);
                end
            end
        end
    end

    // Alias protection: a held probe owns its slot until that output transfers.
    // A reply may resolve the row in the meantime, but no new key can reuse it.
    always @* begin : find_workers
        integer scan_probe, scan_result;
        probe_found = 1'b0;
        drain_found = 1'b0;
        probe_at = '0;
        drain_at = '0;
        for (integer offset = 0; offset < PENDING_DEPTH; offset++) begin
            scan_probe = int'(probe_cursor) + offset;
            if (scan_probe >= PENDING_DEPTH)
                scan_probe = scan_probe - PENDING_DEPTH;
            if (!probe_found && pending_state[scan_probe] == OPEN &&
                !(m_probe_valid && probe_slot == SLOT_W'(scan_probe)) &&
                pending_attempts[scan_probe] < ATTEMPT_W'(MAX_ATTEMPTS) &&
                (pending_attempts[scan_probe] == 0 ||
                 tick - pending_last_probe[scan_probe] >= 32'(RETRY_CYCLES))) begin
                probe_found = 1'b1;
                probe_at = SLOT_W'(scan_probe);
            end
            scan_result = int'(drain_cursor) + offset;
            if (scan_result >= PENDING_DEPTH)
                scan_result = scan_result - PENDING_DEPTH;
            if (!drain_found && pending_state[scan_result] == COMPLETE && used[scan_result] != 0) begin
                drain_found = 1'b1;
                drain_at = SLOT_W'(scan_result);
            end
        end
    end

    // An eligible matching reply gets first use of the table. This preserves
    // a reply that arrives on the last acceptable edge. Unmatched replies
    // alternate with lookups under contention.
    wire incoming_match = s_reply_valid && learn_found && mac_ok;
    assign s_reply_ready = !rst &&
                            (incoming_match || !s_valid || control_priority || drain_found || !response_room);
    assign s_ready = !rst && response_room && !drain_found &&
                     !incoming_match && (!s_reply_valid || !control_priority);

    wire learn_fire = s_reply_valid && s_reply_ready;
    wire lookup_fire = s_valid && s_ready;
    wire learned = learn_fire && learn_found && mac_ok;
    wire [CACHE_W-1:0] cache_store = refresh_found ? refresh_at : victim_at;
    wire new_probe = can_replace_probe && probe_found && !(learned && learn_at == probe_at);
    assign {m_probe_tagged, m_probe_vid, m_probe_ip} = probe_key;

    task automatic publish(
        input logic [2:0] status,
        input logic [COOKIE_W-1:0] cookie,
        input logic [47:0] mac
    );
        begin
            m_valid <= 1'b1;
            m_status <= status;
            m_cookie <= cookie;
            m_mac <= mac;
        end
    endtask

    // Count all simultaneous watchdog expirations without dropping increments.
    logic [31:0] expiring;
    always @* begin
        expiring = 0;
        for (integer row = 0; row < PENDING_DEPTH; row++)
            expiring = expiring + 32'(deadlines[row]);
    end
    wire [32:0] timeout_total = {1'b0,timeouts} + {1'b0,expiring};

    always_ff @(posedge clk) begin
        if (rst) begin
            tick <= '0;
            cache_valid <= '0;
            eviction_pointer <= '0;
            probe_cursor <= '0;
            drain_cursor <= '0;
            probe_slot <= '0;
            probe_key <= '0;
            m_probe_valid <= 1'b0;
            control_priority <= 1'b0;
            m_valid <= 1'b0;
            m_status <= '0;
            m_cookie <= '0;
            m_mac <= '0;
            cache_hits <= '0;
            misses <= '0;
            joined <= '0;
            probes_sent <= '0;
            replies_learned <= '0;
            replies_ignored <= '0;
            timeouts <= '0;
            for (integer row = 0; row < PENDING_DEPTH; row++) begin
                pending_state[row] <= FREE;
                used[row] <= '0;
                head[row] <= '0;
                tail[row] <= '0;
                pending_attempts[row] <= '0;
            end
        end else begin
            tick <= tick + 1'b1;
            timeouts <= timeout_total[32] ? '1 : timeout_total[31:0];
            cache_valid <= cache_alive;
            for (integer row = 0; row < PENDING_DEPTH; row++) begin
                if (deadlines[row]) begin
                    pending_state[row] <= COMPLETE;
                    pending_result[row] <= TIMEOUT;
                    pending_mac[row] <= '0;
                end
            end

            if (m_ready)
                m_valid <= 1'b0;
            if (probe_fire) begin
                m_probe_valid <= 1'b0;
                probes_sent <= inc_sat(probes_sent);
                if (pending_state[probe_slot] == OPEN) begin
                    pending_last_probe[probe_slot] <= tick;
                    pending_attempts[probe_slot] <= pending_attempts[probe_slot] + 1'b1;
                end else if (used[probe_slot] == 0)
                    pending_state[probe_slot] <= FREE;
            end
            if (new_probe) begin
                m_probe_valid <= 1'b1;
                probe_slot <= probe_at;
                probe_key <= pending_key[probe_at];
                probe_cursor <= slot_next(probe_at);
            end

            if (learn_fire) begin
                control_priority <= 1'b0;
                if (learned) begin
                    replies_learned <= inc_sat(replies_learned);
                    pending_state[learn_at] <= COMPLETE;
                    pending_result[learn_at] <= RESOLVED;
                    pending_mac[learn_at] <= s_reply_mac;
                    cache_valid[cache_store] <= 1'b1;
                    cache_key[cache_store] <= reply_key;
                    cache_mac[cache_store] <= s_reply_mac;
                    cache_born[cache_store] <= tick;
                    if (!refresh_found && !evict_found)
                        eviction_pointer <= eviction_pointer == CACHE_W'(CACHE_DEPTH-1)
                                            ? '0 : eviction_pointer + 1'b1;
                end else begin
                    replies_ignored <= inc_sat(replies_ignored);
                end
            end

            if (drain_found && response_room) begin
                publish(pending_result[drain_at], waiters[drain_at][head[drain_at]], pending_mac[drain_at]);
                head[drain_at] <= waiter_next(head[drain_at]);
                used[drain_at] <= used[drain_at] - 1'b1;
                if (used[drain_at] == 1 && (!m_probe_valid || probe_slot != drain_at || probe_fire))
                    pending_state[drain_at] <= FREE;
                drain_cursor <= slot_next(drain_at);
            end else if (lookup_fire) begin
                control_priority <= 1'b1;
                if (!lookup_key_ok)
                    publish(BAD_ADDRESS, s_cookie, '0);
                else if (cache_hit) begin
                    cache_hits <= inc_sat(cache_hits);
                    publish(RESOLVED, s_cookie, cache_mac[cache_hit_at]);
                end else if (join_found) begin
                    if (used[join_at] == COUNT_W'(WAITERS))
                        publish(BUSY, s_cookie, '0);
                    else begin
                        joined <= inc_sat(joined);
                        waiters[join_at][tail[join_at]] <= s_cookie;
                        tail[join_at] <= waiter_next(tail[join_at]);
                        used[join_at] <= used[join_at] + 1'b1;
                    end
                end else if (free_found) begin
                    misses <= inc_sat(misses);
                    pending_state[free_at] <= OPEN;
                    pending_key[free_at] <= request_key;
                    pending_attempts[free_at] <= '0;
                    head[free_at] <= '0;
                    tail[free_at] <= waiter_next('0);
                    used[free_at] <= 1;
                    waiters[free_at][0] <= s_cookie;
                end else begin
                    publish(BUSY, s_cookie, '0);
                end
            end
        end
    end
endmodule

`default_nettype wire
