`timescale 1ns/1ps
`default_nettype none

// Two-way table keyed by {class, tag}. A pending row may contain either
// endpoint, so different CDC queue delays need not reorder the measurement.
module latency_correlator #(
    parameter integer TAG_W = 16,
    parameter integer CLASS_W = 2,
    parameter integer PROBE_W = 2,
    parameter integer INDEX_W = 4,
    parameter integer SCAN_LOG = 3,
    localparam integer SETS = 1 << INDEX_W,
    localparam integer SLOTS = 2 * SETS,
    localparam integer SLOT_W = INDEX_W + 1,
    localparam integer KEY_W = TAG_W + CLASS_W,
    localparam integer RECORD_W = KEY_W + PROBE_W + 65
) (
    input  wire                 clk,
    input  wire                 reset,
    input  wire                 flush,
    input  wire                 armed,
    input  wire [63:0]          now_tick,
    input  wire [63:0]          timeout_ticks,
    input  wire                 s_valid,
    output wire                 s_ready,
    input  wire [RECORD_W-1:0] s_data,
    output logic                m_valid,
    input  wire                 m_ready,
    output logic [2:0]          m_status,
    output logic [TAG_W-1:0]    m_tag,
    output logic [CLASS_W-1:0]  m_class,
    output logic [PROBE_W-1:0]  m_start_probe,
    output logic [PROBE_W-1:0]  m_stop_probe,
    output logic [63:0]         m_start_tick,
    output logic [63:0]         m_stop_tick,
    output logic [63:0]         m_latency,
    output logic [63:0]         m_observed_tick,
    output logic [SLOT_W:0]     pending,
    output logic [31:0]         expired_on_touch
);
    localparam logic [2:0] STORED = 0, MATCH = 1, DUPLICATE = 2, SET_FULL = 3,
                           NEGATIVE = 4, EXPIRED = 5, DISARMED = 6, BAD_TIME = 7;

    logic [SLOTS-1:0] active;
    logic [KEY_W-1:0] keys [0:SLOTS-1];
    logic end_flags [0:SLOTS-1];
    logic [PROBE_W-1:0] probes [0:SLOTS-1];
    logic [63:0] timestamps [0:SLOTS-1];
    logic [63:0] admitted_at [0:SLOTS-1];
    logic [SLOT_W-1:0] scan_slot;
    logic [SCAN_LOG-1:0] scan_count;

    wire [KEY_W-1:0] key;
    wire [PROBE_W-1:0] probe;
    wire end_event;
    wire [63:0] stamp;
    assign {end_event, key, probe, stamp} = s_data;

    function automatic logic [INDEX_W-1:0] index_of(input logic [KEY_W-1:0] full_key);
        logic [INDEX_W-1:0] index;
        begin
            index = '0;
            for (integer bit_no = 0; bit_no < KEY_W; bit_no = bit_no + 1)
                index[bit_no % INDEX_W] = index[bit_no % INDEX_W] ^ full_key[bit_no];
            index_of = index;
        end
    endfunction

    function automatic logic too_old(
        input logic [63:0] arrival,
        input logic [63:0] present,
        input logic [63:0] time_limit
    );
        logic [63:0] age;
        begin
            age = present - arrival;
            too_old = !age[63] && age >= time_limit;
        end
    endfunction

    wire [63:0] time_limit =
        (timeout_ticks == 0 || timeout_ticks[63]) ? 64'd1 : timeout_ticks;
    wire [INDEX_W-1:0] key_index = index_of(key);
    wire [SLOT_W-1:0] a = {key_index, 1'b0};
    wire [SLOT_W-1:0] b = {key_index, 1'b1};
    wire age_a = active[a] && too_old(admitted_at[a], now_tick, time_limit);
    wire age_b = active[b] && too_old(admitted_at[b], now_tick, time_limit);
    wire hit_a = active[a] && !age_a && key == keys[a];
    wire hit_b = active[b] && !age_b && key == keys[b];
    wire found = hit_a || hit_b;
    wire alloc_a = !active[a] || age_a;
    wire alloc_b = !active[b] || age_b;
    wire [SLOT_W-1:0] found_slot = hit_a ? a : b;
    wire [SLOT_W-1:0] allocation = alloc_a ? a : b;
    wire future_stamp = $signed(now_tick - stamp) < 0;
    wire [63:0] start_tick = end_event ? timestamps[found_slot] : stamp;
    wire [63:0] stop_tick = end_event ? stamp : timestamps[found_slot];
    wire [63:0] latency = stop_tick - start_tick;
    wire [PROBE_W-1:0] start_probe = end_event ? probes[found_slot] : probe;
    wire [PROBE_W-1:0] stop_probe = end_event ? probe : probes[found_slot];

    wire space = !m_valid || m_ready;
    wire can_step = space && !reset && !flush;
    wire scan_due = scan_count == '1;
    wire scan_stale = active[scan_slot] && too_old(admitted_at[scan_slot], now_tick, time_limit);
    wire sweep = scan_due && can_step;
    wire accept = s_valid && s_ready;
    assign s_ready = can_step && !scan_due;

    // Read-only telemetry fold, with no dependence on stale table fields.
    always_comb begin
        pending = '0;
        for (integer entry = 0; entry < SLOTS; entry = entry + 1)
            pending = pending + (SLOT_W+1)'(active[entry]);
    end

    always_ff @(posedge clk or posedge reset) begin
        if (reset) begin
            active <= '0;
            scan_slot <= '0;
            scan_count <= '0;
            m_valid <= 1'b0;
            m_status <= '0;
            m_tag <= '0;
            m_class <= '0;
            m_start_probe <= '0;
            m_stop_probe <= '0;
            m_start_tick <= '0;
            m_stop_tick <= '0;
            m_latency <= '0;
            m_observed_tick <= '0;
            expired_on_touch <= '0;
        end else if (flush) begin
            active <= '0;
            scan_slot <= '0;
            scan_count <= '0;
            m_valid <= 1'b0;
            expired_on_touch <= '0;
        end else if (can_step) begin
            m_valid <= 1'b0;
            scan_count <= scan_count + 1'b1;
            if (sweep) begin
                scan_slot <= scan_slot + 1'b1;
                if (scan_stale) begin
                    active[scan_slot] <= 1'b0;
                    m_valid <= 1'b1;
                    m_status <= EXPIRED;
                    {m_class, m_tag} <= keys[scan_slot];
                    m_start_probe <= end_flags[scan_slot] ? '0 : probes[scan_slot];
                    m_stop_probe <= end_flags[scan_slot] ? probes[scan_slot] : '0;
                    m_start_tick <= end_flags[scan_slot] ? '0 : timestamps[scan_slot];
                    m_stop_tick <= end_flags[scan_slot] ? timestamps[scan_slot] : '0;
                    m_latency <= '0;
                    m_observed_tick <= now_tick;
                end
            end else if (accept) begin
                m_valid <= 1'b1;
                {m_class, m_tag} <= key;
                m_start_probe <= end_event ? '0 : probe;
                m_stop_probe <= end_event ? probe : '0;
                m_start_tick <= end_event ? '0 : stamp;
                m_stop_tick <= end_event ? stamp : '0;
                m_latency <= '0;
                m_observed_tick <= now_tick;
                if (!armed) begin
                    m_status <= DISARMED;
                end else if (future_stamp) begin
                    m_status <= BAD_TIME;
                end else if (found) begin
                    if (end_flags[found_slot] == end_event) begin
                        m_status <= DUPLICATE;
                    end else begin
                        active[found_slot] <= 1'b0;
                        m_start_probe <= start_probe;
                        m_stop_probe <= stop_probe;
                        m_start_tick <= start_tick;
                        m_stop_tick <= stop_tick;
                        if (latency[63]) begin
                            m_status <= NEGATIVE;
                        end else begin
                            m_status <= MATCH;
                            m_latency <= latency;
                        end
                    end
                end else if (alloc_a || alloc_b) begin
                    m_status <= STORED;
                    if (active[allocation] && expired_on_touch != '1)
                        expired_on_touch <= expired_on_touch + 1'b1;
                    active[allocation] <= 1'b1;
                    keys[allocation] <= key;
                    end_flags[allocation] <= end_event;
                    probes[allocation] <= probe;
                    timestamps[allocation] <= stamp;
                    admitted_at[allocation] <= now_tick;
                end else begin
                    m_status <= SET_FULL;
                end
            end
        end
    end
endmodule

`default_nettype wire
