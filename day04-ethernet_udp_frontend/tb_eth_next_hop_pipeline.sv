`timescale 1ns/1ps
`default_nettype none

module tb_eth_next_hop_pipeline #(
    parameter integer SLOTS = 3,
    localparam integer SLOT_W = SLOTS > 1 ? $clog2(SLOTS) : 1,
    localparam integer COUNT_W = $clog2(SLOTS+1)
);
    localparam integer ROW_W = 3, EPOCH_W = 3, TAG_W = 12, LIMIT = 1600;
    localparam integer RESULT_W = 129 + TAG_W + ROW_W + EPOCH_W;
    localparam integer ARP_W = SLOT_W + 45;
    logic clk=0, rst=1;
    always #5 clk = !clk;
    logic [63:0] clock_tick=0;
    always @(posedge clk)
        clock_tick <= rst ? 64'd0 : clock_tick + 1'b1;

    logic s_valid=0, s_tagged=0, s_allow_multicast=0;
    wire s_ready;
    logic [TAG_W-1:0] s_tag=0;
    logic [11:0] s_vid=0;
    logic [31:0] s_destination=0;
    wire m_valid, m_tagged;
    logic m_ready=0;
    wire [TAG_W-1:0] m_tag;
    wire [11:0] m_vid;
    wire [31:0] m_destination, m_next_hop;
    wire [47:0] m_mac;
    wire [3:0] m_status;
    wire [ROW_W-1:0] m_row;
    wire [EPOCH_W-1:0] m_epoch;
    wire [63:0] m_start_tick, m_finish_tick;
    wire [COUNT_W-1:0] occupied;
    wire [31:0] unexpected_neighbor_replies, successful_lookups, failed_lookups;

    wire arp_request_valid, arp_request_tagged, arp_result_ready;
    logic arp_request_ready=0, arp_result_valid=0;
    wire [11:0] arp_request_vid;
    wire [31:0] arp_request_ip;
    wire [SLOT_W-1:0] arp_request_slot;
    logic [2:0] arp_result_status=0;
    logic [SLOT_W-1:0] arp_result_slot=0;
    logic [47:0] arp_result_mac=0;

    logic cfg_valid=0, cfg_enabled=1, cfg_tagged=0, cfg_reject=0, commit_valid=0;
    wire cfg_ready, cfg_error, commit_ready;
    logic [ROW_W-1:0] cfg_row=0;
    logic [11:0] cfg_vid=0;
    logic [31:0] cfg_network=0, cfg_gateway=0;
    logic [5:0] cfg_prefix=0;
    wire [EPOCH_W-1:0] active_epoch;

    eth_next_hop_pipeline #(.SLOTS(SLOTS), .ROWS(6), .TAG_W(TAG_W), .EPOCH_W(EPOCH_W)) dut (.*);

    typedef enum logic [1:0] {NEW, ACTIVE, FINISHED} seq_state;
    seq_state requests [0:LIMIT-1];
    logic expected_tagged [0:LIMIT-1];
    logic [11:0] expected_vid [0:LIMIT-1];
    logic [31:0] expected_destination [0:LIMIT-1], expected_hop [0:LIMIT-1];
    logic [3:0] expected_status [0:LIMIT-1];
    logic [47:0] expected_mac [0:LIMIT-1];
    logic [ROW_W-1:0] expected_row [0:LIMIT-1];
    logic [63:0] started [0:LIMIT-1], finished [0:LIMIT-1];
    logic expected_arp;
    logic [3:0] next_status;
    logic [31:0] next_hop;
    logic [47:0] next_mac;
    logic [ROW_W-1:0] next_row;
    logic [2:0] next_arp_status;
    integer next_delay;
    integer queued_tags [0:LIMIT-1], delays [0:LIMIT-1];
    logic [2:0] arp_codes [0:LIMIT-1];
    integer arp_push=0, arp_pop=0;
    logic [SLOTS-1:0] pending='0;
    logic [63:0] due [0:SLOTS-1];
    integer pending_tag [0:SLOTS-1];
    integer sent=0, received=0, successes=0, delayed=0, ready_stalls=0, result_stalls=0;
    integer max_occupied=0, highest_seen=-1, reordered=0;
    integer status_hits [0:7];
    logic accepted=0, result_held=0, arp_held=0, stray=0, driven_stray=0, enable_driver=0;
    logic [31:0] lfsr = 32'h826731af;
    wire [RESULT_W-1:0] result = {
        m_tag,m_tagged,m_vid,m_destination,m_next_hop,m_mac,m_status,m_row,m_epoch
    };
    logic [RESULT_W-1:0] saved;
    logic [127:0] saved_ticks;
    wire [ARP_W-1:0] arp_offer = {
        arp_request_slot,arp_request_tagged,arp_request_vid,arp_request_ip
    };
    logic [ARP_W-1:0] saved_arp;
    string folder;

    always @(negedge clk) begin : supply_neighbor
        integer chosen;
        if (!rst && enable_driver) begin
            lfsr = {lfsr[30:0],lfsr[31]^lfsr[21]^lfsr[1]^lfsr[0]};
            m_ready = lfsr[8] && clock_tick%29 < 22;
            arp_request_ready = lfsr[17] || clock_tick%13 == 0;
            chosen = -1;
            for (integer slot=0; slot<SLOTS; slot=slot+1)
                if (pending[slot] && due[slot] <= clock_tick &&
                    (chosen == -1 || pending_tag[slot] > pending_tag[chosen]))
                    chosen = slot;
            driven_stray = stray;
            arp_result_valid = stray || (chosen != -1 && lfsr[3]);
            if (stray) begin
                arp_result_slot = '1;
                arp_result_status = 0;
                arp_result_mac = '1;
            end else if (chosen != -1) begin
                arp_result_slot = SLOT_W'(chosen);
                arp_result_status = arp_codes[pending_tag[chosen]];
                arp_result_mac = arp_result_status == 0 ? expected_mac[pending_tag[chosen]] : '1;
            end
        end
    end

    always @(posedge clk) begin : audit_transactions
        integer looked_up;
        if (!rst && enable_driver) begin
            accepted <= s_valid && s_ready;
            ready_stalls <= ready_stalls + (s_valid && !s_ready);
            result_stalls <= result_stalls + (m_valid && !m_ready);
            if (int'(occupied) > max_occupied)
                max_occupied <= int'(occupied);
            if (result_held && (!m_valid || result !== saved ||
                                {m_start_tick,m_finish_tick} !== saved_ticks))
                $fatal(1,"changed result under backpressure");
            if (arp_held && (!arp_request_valid || arp_offer !== saved_arp))
                $fatal(1,"changed ARP request under backpressure");
            result_held <= m_valid && !m_ready;
            arp_held <= arp_request_valid && !arp_request_ready;
            saved <= result;
            saved_ticks <= {m_start_tick,m_finish_tick};
            saved_arp <= arp_offer;

            if (s_valid && s_ready) begin
                if (requests[s_tag] != NEW) $fatal(1,"query reused an active test tag");
                sent <= sent+1;
                requests[s_tag] <= ACTIVE;
                started[s_tag] <= clock_tick;
                if (expected_arp) begin
                    queued_tags[arp_push] <= int'(s_tag);
                    delays[s_tag] <= next_delay;
                    arp_codes[s_tag] <= next_arp_status;
                    arp_push <= arp_push+1;
                end else begin
                    finished[s_tag] <= clock_tick + 64'(s_destination[31:28] != 4'he);
                end
            end
            if (arp_request_valid && arp_request_ready) begin
                if (arp_pop >= arp_push || pending[arp_request_slot])
                    $fatal(1,"unexpected new neighbor request or alias");
                looked_up = queued_tags[arp_pop];
                if ({arp_request_tagged,arp_request_vid,arp_request_ip} !==
                    {expected_tagged[looked_up],expected_vid[looked_up],expected_hop[looked_up]})
                    $fatal(1,"neighbor request differs from IPv4 network oracle");
                arp_pop <= arp_pop+1;
                pending[arp_request_slot] <= 1;
                pending_tag[arp_request_slot] <= looked_up;
                due[arp_request_slot] <= clock_tick+64'(delays[looked_up]);
            end
            if (arp_result_valid && arp_result_ready && !driven_stray) begin
                pending[arp_result_slot] <= 0;
                finished[pending_tag[arp_result_slot]] <= clock_tick;
                delayed <= delayed+1;
            end
            if (m_valid && m_ready) begin
                received <= received+1;
                successes <= successes+(m_status == 0);
                if (int'(m_tag) < highest_seen)
                    reordered <= reordered+1;
                else
                    highest_seen <= int'(m_tag);
                if (requests[m_tag] != ACTIVE ||
                    {m_tagged,m_vid,m_destination,m_next_hop,m_mac,m_status,m_row,m_epoch} !==
                    {expected_tagged[m_tag],expected_vid[m_tag],expected_destination[m_tag],expected_hop[m_tag],
                     expected_mac[m_tag],expected_status[m_tag],expected_row[m_tag],EPOCH_W'(1)} ||
                    {m_start_tick,m_finish_tick} !== {started[m_tag],finished[m_tag]})
                    $fatal(1,"oracle result mismatch at cycle %0d tag %0d, got %x expected status=%d row=%d",
                           clock_tick,m_tag,result,expected_status[m_tag],expected_row[m_tag]);
                requests[m_tag] <= FINISHED;
                status_hits[m_status] <= status_hits[m_status] + 1;
            end
        end
    end

    initial begin
        integer route_file, request_file, rc;
        if (!$value$plusargs("vectors=%s",folder)) $fatal(1,"missing route vectors");
        route_file = $fopen({folder,"/rows.hex"},"r");
        request_file = $fopen({folder,"/requests.hex"},"r");
        if (!route_file || !request_file) $fatal(1,"open route test inputs");
        for (integer i=0;i<LIMIT;i=i+1) requests[i] = NEW;
        for (integer i=0;i<8;i=i+1) status_hits[i] = 0;
        repeat (3) @(negedge clk);
        rst = 0;
        rc = $fscanf(route_file,"%h %h %h %h %h %h %h\n",
                     cfg_row,cfg_tagged,cfg_vid,cfg_network,cfg_prefix,cfg_gateway,cfg_reject);
        while (rc == 7) begin
            cfg_valid = 1;
            @(posedge clk);
            if (!cfg_ready) $fatal(1,"route write stalled");
            @(negedge clk);
            if (cfg_error) $fatal(1,"route oracle supplied incorrect configuration");
            rc = $fscanf(route_file,"%h %h %h %h %h %h %h\n",
                         cfg_row,cfg_tagged,cfg_vid,cfg_network,cfg_prefix,cfg_gateway,cfg_reject);
        end
        if (rc != -1) $fatal(1,"bad route row");
        cfg_valid = 0;
        commit_valid = 1;
        @(posedge clk);
        if (!commit_ready) $fatal(1,"initial policy did not commit");
        @(negedge clk);
        commit_valid = 0;
        // Drive a stray neighbor response before any slot owns an exchange.
        #1;
        stray = 1;
        enable_driver = 1;
        @(posedge clk);
        @(negedge clk);
        #1;
        stray = 0;
        repeat (2) @(negedge clk);
        #1;
        if (unexpected_neighbor_replies != 1 || m_valid)
            $fatal(1,"stray neighbor response created a completion");

        rc = $fscanf(request_file,"%h %h %h %h %h %h %h %h %h %h %h %h\n",
                     s_tag,s_tagged,s_vid,s_destination,s_allow_multicast,next_status,next_hop,
                     next_mac,next_row,expected_arp,next_arp_status,next_delay);
        while (rc == 12) begin
            if (int'(s_tag) >= LIMIT) $fatal(1,"test tag exceeds oracle storage");
            expected_tagged[s_tag] = s_tagged;
            expected_vid[s_tag] = s_tagged ? s_vid : 12'd0;
            expected_destination[s_tag] = s_destination;
            expected_hop[s_tag] = next_hop;
            expected_status[s_tag] = next_status;
            expected_mac[s_tag] = next_mac;
            expected_row[s_tag] = next_row;
            s_valid = 1;
            @(negedge clk);
            while (!accepted) @(negedge clk);
            if (lfsr[4] && lfsr[6]) begin
                s_valid = 0;
                @(negedge clk);
            end
            rc = $fscanf(request_file,"%h %h %h %h %h %h %h %h %h %h %h %h\n",
                         s_tag,s_tagged,s_vid,s_destination,s_allow_multicast,next_status,next_hop,
                         next_mac,next_row,expected_arp,next_arp_status,next_delay);
        end
        if (rc != -1) $fatal(1,"bad query row");
        s_valid = 0;
        while (received != sent) @(negedge clk);
        repeat (10) @(negedge clk);
        if (occupied || pending || m_valid || arp_push != arp_pop || arp_pop != delayed ||
            successful_lookups != 32'(successes) || failed_lookups != 32'(received-successes) ||
            max_occupied != SLOTS || unexpected_neighbor_replies != 1 ||
            (SLOTS == 1 ? reordered != 0 : reordered == 0))
            $fatal(1,"drain occupied=%0d pending=%x valid=%b, ARP=%0d/%0d/%0d, good=%0d/%0d fail=%0d/%0d max=%0d stray=%0d reordered=%0d",
                   occupied,pending,m_valid,arp_push,arp_pop,delayed,successful_lookups,successes,
                   failed_lookups,received-successes,max_occupied,unexpected_neighbor_replies,reordered);
        for (integer i=0;i<8;i=i+1)
            if (!status_hits[i]) $fatal(1,"missing status %0d",i);
        $display("PASS next-hop slots=%0d queries=%0d ARP=%0d reorder=%0d held=%0d/%0d cycles=%0d",
                 SLOTS,sent,delayed,reordered,ready_stalls,result_stalls,clock_tick);
        $finish;
    end

    initial begin
        #2000000;
        $fatal(1,"next-hop scoreboard timeout");
    end
endmodule

`default_nettype wire
