`timescale 1ns/1ps
`default_nettype none

// Steer a common MAC RX stream to IPv4 or ARP without changing a beat.
// A 75-bit beat is {bad, first, last, keep[7:0], data[63:0]}.
module eth_mac_dispatch (
    input  wire         clk,
    input  wire         rst,
    input  wire [63:0]  clock_tick,
    input  wire         s_valid,
    output logic        s_ready,
    input  wire [74:0]  s_beat,
    output logic [1:0]  m_valid,
    input  wire [1:0]   m_ready,
    output logic [74:0] m_beat,
    output wire [63:0]  m_tick,
    output logic [31:0] ipv4_frames,
    output logic [31:0] arp_frames,
    output logic [31:0] other_frames,
    output logic [31:0] malformed_prefixes
);
    localparam logic [1:0] IPV4 = 0, ARP = 1, OTHER = 2, MALFORMED = 3;
    localparam logic [15:0] ETH_IP = 16'h0800, ETH_ARP = 16'h0806;
    localparam logic [15:0] VLAN = 16'h8100, SERVICE = 16'h88a8;
    typedef enum logic [1:0] {COLLECT, REPLAY, STREAM, DISCARD} phase_t;
    phase_t phase;
    logic [74:0] leading [0:2];
    logic [1:0] leading_count, index;
    logic [63:0] first_tick;
    logic [15:0] outer_type;
    logic [1:0] target, decision;
    logic determined;
    wire [7:0] keep = s_beat[71:64];
    wire is_first = s_beat[73];
    wire is_last = s_beat[72];
    wire [15:0] type_at_lane0 = {s_beat[7:0],s_beat[15:8]};
    wire [15:0] type_at_lane4 = {s_beat[39:32],s_beat[47:40]};
    wire keep_prefix = keep != 0 && (keep & (keep+1'b1)) == 0;
    wire shape_valid = keep_prefix && (is_last || keep == 8'hff) &&
                       is_first == (leading_count == 0);

    function automatic logic vlan_type(input logic [15:0] value);
        vlan_type = value == VLAN || value == SERVICE;
    endfunction

    function automatic logic [1:0] choose(
        input logic [15:0] ethertype,
        input logic allow_arp
    );
        if (ethertype == ETH_IP)
            choose = IPV4;
        else if (ethertype == ETH_ARP && allow_arp)
            choose = ARP;
        else if (vlan_type(ethertype))
            choose = MALFORMED;
        else
            choose = OTHER;
    endfunction

    function automatic logic [31:0] increment(input logic [31:0] count);
        increment = (&count) ? count : count + 1'b1;
    endfunction

    // The three relevant EtherType locations are bytes 12, 16, and 20.
    // Report unsupported stacked ARP as OTHER, as the ARP MAC client only
    // accepts untagged frames and one customer VLAN tag.
    always @* begin
        determined = 1'b1;
        decision = MALFORMED;
        if (shape_valid) begin
            case (leading_count)
                0: determined = is_last;
                1: if (keep[5]) begin
                    if (vlan_type(type_at_lane4))
                        determined = is_last;
                    else
                        decision = choose(type_at_lane4, 1'b1);
                end
                2: if (keep[1]) begin
                    if (!vlan_type(type_at_lane0))
                        decision = choose(type_at_lane0, outer_type == VLAN);
                    else if (keep[5])
                        decision = choose(type_at_lane4, 1'b0);
                end
                default: ;
            endcase
        end
    end

    assign m_tick = first_tick;
    always @* begin
        s_ready = 0;
        m_valid = '0;
        m_beat = '0;
        if (!rst) begin
            case (phase)
                COLLECT, DISCARD: s_ready = 1'b1;
                REPLAY: begin
                    m_valid[target[0]] = 1'b1;
                    m_beat = leading[index];
                end
                STREAM: begin
                    s_ready = m_ready[target[0]];
                    m_valid[target[0]] = s_valid;
                    m_beat = s_valid ? s_beat : 75'd0;
                end
                default: ;
            endcase
        end
    end

    wire accepted = s_valid && s_ready;
    wire delivered = |(m_valid & m_ready);
    wire forwarded = delivered && m_beat[72];
    wire discarded = accepted && is_last &&
                     (phase == DISCARD || (phase == COLLECT && determined && decision[1]));
    wire [1:0] completed_target = phase == COLLECT ? decision : target;

    always_ff @(posedge clk) begin
        if (rst) begin
            phase <= COLLECT;
            leading_count <= '0;
            index <= '0;
            first_tick <= '0;
            outer_type <= '0;
            target <= MALFORMED;
            ipv4_frames <= '0;
            arp_frames <= '0;
            other_frames <= '0;
            malformed_prefixes <= '0;
        end else begin
            if (forwarded || discarded) begin
                case (completed_target)
                    IPV4: ipv4_frames <= increment(ipv4_frames);
                    ARP: arp_frames <= increment(arp_frames);
                    OTHER: other_frames <= increment(other_frames);
                    MALFORMED: malformed_prefixes <= increment(malformed_prefixes);
                    default: ;
                endcase
            end
            case (phase)
                COLLECT: if (accepted) begin
                    leading[leading_count] <= s_beat;
                    if (leading_count == 0)
                        first_tick <= clock_tick;
                    if (leading_count == 1)
                        outer_type <= type_at_lane4;
                    if (!determined) begin
                        leading_count <= leading_count + 1'b1;
                    end else begin
                        target <= decision;
                        index <= '0;
                        if (!decision[1])
                            phase <= REPLAY;
                        else if (is_last)
                            leading_count <= '0;
                        else
                            phase <= DISCARD;
                    end
                end
                REPLAY: if (delivered) begin
                    if (m_beat[72]) begin
                        phase <= COLLECT;
                        leading_count <= '0;
                    end else if (index == leading_count) begin
                        phase <= STREAM;
                    end else begin
                        index <= index + 1'b1;
                    end
                end
                STREAM: if (forwarded) begin
                    phase <= COLLECT;
                    leading_count <= '0;
                end
                DISCARD: if (discarded) begin
                    phase <= COLLECT;
                    leading_count <= '0;
                end
                default: phase <= COLLECT;
            endcase
        end
    end

`ifdef FORMAL_DISPATCH
    logic initialized = 1'b0;
    always_ff @(posedge clk) begin
        initialized <= 1'b1;
        if (!initialized)
            assume (rst);
        if (initialized && !rst && !$past(rst)) begin
            if ($past(s_valid && !s_ready)) begin
                assume (s_valid && $stable(s_beat));
            end
            assert (m_valid != 2'b11);
            assert (leading_count < 3 && index < 3);
            if ($past((|m_valid) && !delivered)) begin
                assert ($stable({m_valid,m_beat,m_tick}));
            end
            if (phase == REPLAY || phase == STREAM)
                assert (target == IPV4 || target == ARP);
            cover (forwarded && target == IPV4);
            cover (forwarded && target == ARP);
            cover (discarded && completed_target == MALFORMED);
        end
    end
`endif
endmodule

`default_nettype wire
