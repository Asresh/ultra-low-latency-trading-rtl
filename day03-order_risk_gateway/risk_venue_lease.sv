`timescale 1ns/1ps
`default_nettype none

// A new lease requires a strictly increasing epoch. A fault invalidates the
// current lease without rolling the epoch backward or clearing risk state.
module risk_venue_lease #(
    parameter integer VENUES = 4,
    parameter integer LEASE_W = 16,
    parameter integer TIMER_W = 16,
    localparam integer VENUE_W = $clog2(VENUES+1)
) (
    input wire clk, rst, time_tick,
    input wire [VENUES-1:0] link_loss,
    input wire trip,
    input wire [VENUE_W-1:0] trip_venue,

    input wire heartbeat_valid,
    output wire heartbeat_ready,
    input wire [VENUE_W-1:0] heartbeat_venue,
    input wire [LEASE_W-1:0] heartbeat_epoch,

    input wire g_valid,
    output wire g_ready,
    input wire [1:0] g_op,
    input wire [VENUE_W-1:0] g_venue,
    input wire [LEASE_W-1:0] g_epoch,
    input wire [TIMER_W-1:0] g_timeout,
    output wire g_m_valid,
    input wire g_m_ready,
    output logic [2:0] g_m_status,
    output logic [1:0] g_m_op,
    output logic [VENUE_W-1:0] g_m_venue,
    output logic [LEASE_W-1:0] g_m_epoch,
    output logic [TIMER_W-1:0] g_m_countdown,
    output logic g_m_active,

    output wire [VENUES-1:0] allowed,
    output wire [VENUES-1:0] installed,
    output wire [VENUES*LEASE_W-1:0] effective_epochs,
    output logic [31:0] ignored_heartbeats,
    output logic [31:0] lease_expirations
);
    localparam logic [1:0] ARM = 0, FENCE = 1, READ = 2;
    localparam logic [2:0] OK = 0, BAD_VENUE = 1, BAD_OP = 2, BAD_FIELDS = 3,
                           BAD_EPOCH = 4, LINK_DOWN = 5;
    localparam integer INDEX_W = VENUES <= 1 ? 1 : $clog2(VENUES);
    logic [VENUES-1:0] active, active_next;
    logic [LEASE_W-1:0] epoch [0:VENUES-1], epoch_next [0:VENUES-1];
    logic [TIMER_W-1:0] reload [0:VENUES-1];
    logic [TIMER_W-1:0] timer [0:VENUES-1], timer_next [0:VENUES-1];
    wire [VENUES-1:0] matched_heartbeat, expires;
    wire [INDEX_W-1:0] index = VENUES == 1 ? '0 : INDEX_W'(g_venue);
    wire valid_index = int'(g_venue) < VENUES;
    wire g_fire = g_valid && g_ready;
    logic [2:0] status;
    logic reply_valid;
    logic [31:0] expiring;
    wire [32:0] new_expirations = {1'b0,lease_expirations} + {1'b0,expiring};

    assign installed = active;
    assign heartbeat_ready = !rst;
    assign g_ready = !rst && (!reply_valid || g_m_ready);
    assign g_m_valid = !rst && reply_valid;

    always @* begin
        status = OK;
        if (!valid_index)
            status = BAD_VENUE;
        else if (g_op > READ)
            status = BAD_OP;
        else if ((g_op == ARM && g_timeout == 0) ||
                 (g_op != ARM && g_timeout != 0) || (g_op == READ && g_epoch != 0))
            status = BAD_FIELDS;
        else if ((g_op == ARM && g_epoch <= epoch[index]) ||
                 (g_op == FENCE && g_epoch != epoch[index]))
            status = BAD_EPOCH;
        else if (g_op == ARM && (link_loss[index] || (trip && trip_venue == g_venue)))
            status = LINK_DOWN;
    end

    for (genvar v = 0; v < VENUES; v++) begin : g_venue_state
        wire configure = g_fire && status == OK && g_venue == VENUE_W'(v);
        wire arm = configure && g_op == ARM;
        wire fence = configure && g_op == FENCE;
        wire unhealthy = link_loss[v] || (trip && trip_venue == VENUE_W'(v));
        assign matched_heartbeat[v] = heartbeat_valid && heartbeat_ready && active[v] &&
            heartbeat_venue == VENUE_W'(v) && heartbeat_epoch == epoch[v] && !unhealthy && !fence && !arm;
        assign expires[v] = active[v] && !unhealthy && !fence && !arm && time_tick &&
            !matched_heartbeat[v] && timer[v] == TIMER_W'(1);

        // Only the installed lease can authorize actions. Changing its epoch
        // or closing it revokes the old lease on that very edge.
        assign allowed[v] = !rst && active[v] && !unhealthy && !expires[v] && !fence && !arm;
        assign effective_epochs[v*LEASE_W +: LEASE_W] = epoch[v];

        always @* begin
            active_next[v] = active[v];
            epoch_next[v] = epoch[v];
            timer_next[v] = timer[v];
            if (unhealthy || expires[v] || fence) begin
                active_next[v] = 1'b0;
                timer_next[v] = '0;
            end else if (arm) begin
                active_next[v] = 1'b1;
                epoch_next[v] = g_epoch;
                timer_next[v] = g_timeout;
            end else if (matched_heartbeat[v])
                timer_next[v] = reload[v];
            else if (active[v] && time_tick)
                timer_next[v] = timer[v] - 1'b1;
        end

        always_ff @(posedge clk) begin
            if (rst) begin
                active[v] <= 1'b0;
                epoch[v] <= '0;
                timer[v] <= '0;
                reload[v] <= '0;
            end else begin
                active[v] <= active_next[v];
                epoch[v] <= epoch_next[v];
                timer[v] <= timer_next[v];
                if (arm)
                    reload[v] <= g_timeout;
            end
        end
    end

    always_comb begin
        expiring = '0;
        for (integer v = 0; v < VENUES; v++)
            expiring += 32'(expires[v]);
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            reply_valid <= 1'b0;
            g_m_status <= '0;
            g_m_op <= '0;
            g_m_venue <= '0;
            g_m_epoch <= '0;
            g_m_countdown <= '0;
            g_m_active <= 1'b0;
            ignored_heartbeats <= '0;
            lease_expirations <= '0;
        end else begin
            if (heartbeat_valid && !(|matched_heartbeat) && !(&ignored_heartbeats))
                ignored_heartbeats <= ignored_heartbeats + 1'b1;
            lease_expirations <= new_expirations[32] ? '1 : new_expirations[31:0];

            if (g_ready) begin
                reply_valid <= g_valid;
                if (g_valid) begin
                    g_m_status <= status;
                    g_m_op <= g_op;
                    g_m_venue <= g_venue;
                    g_m_epoch <= valid_index ? epoch_next[index] : '0;
                    g_m_countdown <= valid_index ? timer_next[index] : '0;
                    g_m_active <= valid_index && active_next[index];
                end
            end
        end
    end

`ifdef FORMAL
    always_ff @(posedge clk) begin
        if (!rst && !$past(rst)) begin
            if ($past(g_m_valid && !g_m_ready))
                assert(g_m_valid && $stable({g_m_status,g_m_op,g_m_venue,g_m_epoch,
                                           g_m_countdown,g_m_active}));
            for (integer v = 0; v < VENUES; v++) begin
                assert(epoch[v] >= $past(epoch[v]));
                if (allowed[v])
                    assert(timer[v] != 0 && !link_loss[v]);
            end
        end
    end
`endif
endmodule

`default_nettype wire
