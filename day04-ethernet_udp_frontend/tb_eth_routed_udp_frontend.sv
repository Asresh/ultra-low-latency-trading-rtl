`timescale 1ns/1ps
`default_nettype none

module tb_eth_routed_udp_frontend;
    localparam integer EPOCH_W = 2, ROW_W = 3;
    localparam logic [31:0] PEER = 32'hc0000214, OTHER_PEER = 32'hc000021e;
    localparam logic [31:0] GROUP = 32'hef810203, UNREACHABLE = 32'hc000024d;
    localparam integer STATUS_W = 160 + EPOCH_W + ROW_W;
    logic clk = 0;
    always #5 clk = !clk;

    logic rst = 1, mac_rx_valid = 0, mac_tx_ready = 0, mac_pause = 0;
    logic [74:0] mac_rx_beat = '0;
    wire mac_rx_ready, mac_tx_valid;
    wire [73:0] mac_tx_beat;
    wire [1:0] mac_tx_source;

    logic rule_write_valid = 0, rule_commit_valid = 0;
    logic [2:0] rule_write_index = '0;
    logic [255:0] rule_write_data = '0;
    wire rule_write_ready, rule_commit_ready;
    wire [7:0] rule_epoch;
    wire rx_info_valid, rx_payload_valid, rx_payload_first, rx_payload_last;
    logic rx_info_ready = 0, rx_payload_ready = 0;
    wire [383:0] rx_info_data;
    wire [63:0] rx_payload_data;
    wire [7:0] rx_payload_keep;

    logic [1:0] tx_desc_valid = 0, tx_payload_valid = 0;
    wire [1:0] tx_desc_ready, tx_payload_ready;
    logic [639:0] tx_desc_data = '0;
    logic [1:0] tx_allow_multicast = 0, tx_require_current_epoch = 0;
    logic [127:0] tx_payload_data = '0;
    logic [15:0] tx_payload_keep = '0;
    logic [1:0] tx_payload_first = 0, tx_payload_last = 0;
    logic tx_status_ready = 0;
    wire tx_status_valid;
    wire [127:0] tx_status_data;
    wire [31:0] tx_status_next_hop;
    wire [ROW_W-1:0] tx_status_route_row;
    wire [EPOCH_W-1:0] tx_status_epoch;

    logic cfg_valid = 0, commit_valid = 0, cfg_enabled = 1, cfg_tagged = 1, cfg_reject = 0;
    logic [ROW_W-1:0] cfg_row = '0;
    logic [11:0] cfg_vid = '0;
    logic [31:0] cfg_network = '0, cfg_gateway = '0;
    logic [5:0] cfg_prefix = '0;
    wire cfg_ready, cfg_error, commit_ready;
    wire [EPOCH_W-1:0] active_epoch;

    logic status_snapshot = 0;
    logic [4:0] status_index = '0;
    wire [31:0] status_read_data, status_snapshot_id;

    eth_routed_udp_frontend #(
        .MAX_FRAME(256), .MAX_PAYLOAD(64), .ROUTE_ROWS(6), .ROUTE_EPOCH_W(EPOCH_W),
        .RETRY_CYCLES(160), .MAX_ATTEMPTS(2)
    ) dut (.*);

    integer cycle = 0, trace_fd, physical_frames = 0, arp_frames = 0;
    integer mac_stalls = 0, reports = 0, report_stalls = 0;
    logic mac_held = 0, status_held = 0;
    logic [75:0] saved_mac;
    logic [STATUS_W-1:0] saved_status;
    wire [STATUS_W-1:0] status_line = {
        tx_status_data,tx_status_next_hop,tx_status_route_row,tx_status_epoch
    };
    string directory;

    always @(negedge clk)
        mac_tx_ready <= !rst && !mac_pause && (cycle%7 < 4);

    always @(posedge clk) begin
        if (rst) begin
            cycle <= 0;
            mac_held <= 0;
            status_held <= 0;
        end else begin
            cycle <= cycle + 1;
            if (mac_held && (!mac_tx_valid || {mac_tx_source,mac_tx_beat} !== saved_mac))
                $fatal(1,"MAC output changed under backpressure at %0d",cycle);
            if (status_held && (!tx_status_valid || status_line !== saved_status))
                $fatal(1,"routed completion changed under backpressure at %0d",cycle);
            mac_held <= mac_tx_valid && !mac_tx_ready;
            status_held <= tx_status_valid && !tx_status_ready;
            saved_mac <= {mac_tx_source,mac_tx_beat};
            saved_status <= status_line;
            mac_stalls <= mac_stalls + (mac_tx_valid && !mac_tx_ready);
            report_stalls <= report_stalls + (tx_status_valid && !tx_status_ready);
            reports <= reports + (tx_status_valid && tx_status_ready);
            if (mac_tx_valid && mac_tx_ready) begin
                $fdisplay(trace_fd,"%0d %0d %016h %02h %b %b 0",cycle,mac_tx_source,
                          mac_tx_beat[63:0],mac_tx_beat[71:64],mac_tx_beat[73],mac_tx_beat[72]);
                if (mac_tx_beat[72]) begin
                    physical_frames <= physical_frames + 1;
                    arp_frames <= arp_frames + int'(mac_tx_source == 0);
                end
            end
        end
    end

    task automatic configure(
        input logic [ROW_W-1:0] row,
        input logic [11:0] vlan,
        input logic [31:0] network,
        input logic [5:0] prefix,
        input logic [31:0] gateway,
        input logic reject
    );
        @(negedge clk);
        {cfg_valid,cfg_row,cfg_vid,cfg_network,cfg_prefix,cfg_gateway,cfg_reject} =
            {1'b1,row,vlan,network,prefix,gateway,reject};
        @(posedge clk);
        if (!cfg_ready) $fatal(1,"route configuration was not accepted");
        @(negedge clk);
        cfg_valid = 0;
        if (cfg_error) $fatal(1,"route configuration rejected row %0d",row);
    endtask

    task automatic load_routes;
        configure(0,24,32'hc0000200,24,0,0);
        configure(1,24,0,0,PEER,0);
        configure(2,24,32'hc6336463,32,0,1);
        configure(3,25,0,0,OTHER_PEER,0);
    endtask

    task automatic commit_routes;
        @(negedge clk);
        commit_valid = 1;
        @(posedge clk);
        if (!commit_ready) $fatal(1,"route commit was not accepted");
        @(negedge clk);
        commit_valid = 0;
    endtask

    task automatic rule_enable;
        integer fd;
        logic [255:0] rule_value;
        fd = $fopen({directory,"/permit.rule"},"r");
        if (!fd || $fscanf(fd,"%h",rule_value) != 1) $fatal(1,"read rule file");
        $fclose(fd);
        @(negedge clk);
        rule_write_valid = 1;
        rule_write_data = rule_value;
        @(posedge clk);
        if (!rule_write_ready) $fatal(1,"rule write not accepted");
        @(negedge clk);
        rule_write_valid = 0;
        rule_commit_valid = 1;
        @(posedge clk);
        if (!rule_commit_ready) $fatal(1,"rule commit not accepted");
        @(negedge clk);
        rule_commit_valid = 0;
    endtask

    task automatic send_frame(input string name, output logic [63:0] first_cycle);
        integer fd, read;
        logic [63:0] data;
        logic [7:0] keep;
        logic first, last, bad;
        fd = $fopen({directory,"/",name,".rx"},"r");
        if (!fd) $fatal(1,"open inbound frame %s",name);
        read = $fscanf(fd,"%h %h %b %b %b\n",data,keep,first,last,bad);
        if (read != 5) $fatal(1,"empty inbound frame %s",name);
        @(negedge clk);
        while (read == 5) begin
            mac_rx_valid = 1;
            mac_rx_beat = {bad,first,last,keep,data};
            @(posedge clk);
            while (!mac_rx_ready) @(posedge clk);
            if (first)
                first_cycle = 64'(cycle);
            @(negedge clk);
            read = $fscanf(fd,"%h %h %b %b %b\n",data,keep,first,last,bad);
        end
        mac_rx_valid = 0;
        if (read != -1) $fatal(1,"invalid inbound row %s",name);
        $fclose(fd);
    endtask

    task automatic descriptor(input integer lane, input string name, input logic group_ok, fence);
        integer fd;
        logic [319:0] request_word;
        fd = $fopen({directory,"/",name,".desc"},"r");
        if (!fd || $fscanf(fd,"%h",request_word) != 1) $fatal(1,"open descriptor %s",name);
        $fclose(fd);
        @(negedge clk);
        tx_desc_data[320*lane+:320] = request_word;
        tx_allow_multicast[lane] = group_ok;
        tx_require_current_epoch[lane] = fence;
        tx_desc_valid[lane] = 1;
        @(posedge clk);
        while (!tx_desc_ready[lane]) @(posedge clk);
        @(negedge clk);
        tx_desc_valid[lane] = 0;
    endtask

    task automatic body(input integer lane, input string name);
        integer fd, read;
        logic [63:0] data;
        logic [7:0] keep;
        logic first, last, bad;
        fd = $fopen({directory,"/",name,".body"},"r");
        if (!fd) $fatal(1,"open transmit body %s",name);
        read = $fscanf(fd,"%h %h %b %b %b\n",data,keep,first,last,bad);
        @(negedge clk);
        while (read == 5) begin
            tx_payload_data[64*lane+:64] = data;
            tx_payload_keep[8*lane+:8] = keep;
            tx_payload_first[lane] = first;
            tx_payload_last[lane] = last;
            tx_payload_valid[lane] = 1;
            @(posedge clk);
            while (!tx_payload_ready[lane]) begin
                if (tx_status_valid && int'(tx_status_data[112]) == lane)
                    $fatal(1,"completion preceded the end of body %s",name);
                @(posedge clk);
            end
            @(negedge clk);
            read = $fscanf(fd,"%h %h %b %b %b\n",data,keep,first,last,bad);
        end
        tx_payload_valid[lane] = 0;
        if (read != -1) $fatal(1,"invalid transmit body row %s",name);
        $fclose(fd);
    endtask

    task automatic report(
        input logic lane,
        input logic [31:0] cookie,
        input logic [7:0] status,
        input logic [15:0] bytes,
        input logic [31:0] next_hop,
        input logic [ROW_W-1:0] row,
        input logic [EPOCH_W-1:0] epoch
    );
        while (!tx_status_valid) @(negedge clk);
        repeat (4) begin
            if ({tx_status_data[127:64],tx_status_next_hop,tx_status_route_row,tx_status_epoch} !==
                {status,7'd0,lane,cookie,bytes,next_hop,row,epoch})
                $fatal(1,"bad report cookie=%x actual=%x next=%x row=%d epoch=%d",
                       cookie,tx_status_data[127:64],tx_status_next_hop,tx_status_route_row,tx_status_epoch);
            @(negedge clk);
        end
        tx_status_ready = 1;
        @(negedge clk);
        tx_status_ready = 0;
    endtask

    task automatic receive_udp(input logic [7:0] status, input logic [63:0] stamp);
        logic [383:0] received;
        while (!rx_info_valid) @(negedge clk);
        received = rx_info_data;
        repeat (4) begin
            @(negedge clk);
            if (rx_info_data !== received || !rx_info_valid)
                $fatal(1,"held UDP receive metadata changed");
        end
        if (received[383:376] !== status || received[351:288] !== stamp ||
            received[367:360] !== 8'd1)
            $fatal(1,"receive status, epoch or physical timestamp mismatch");
        if (status == 0 &&
            {received[375:368],received[359:352],received[95:80],received[7:6]} !==
            {8'd5,8'd22,16'd9,2'd2})
            $fatal(1,"QinQ frame classification or payload length mismatch");
        rx_info_ready = 1;
        @(negedge clk);
        rx_info_ready = 0;
    endtask

    task automatic read_body;
        integer fd, read;
        logic [63:0] data;
        logic [7:0] keep;
        logic first, last, bad;
        fd = $fopen({directory,"/ipv4.expected"},"r");
        if (!fd) $fatal(1,"open expected UDP body");
        read = $fscanf(fd,"%h %h %b %b %b\n",data,keep,first,last,bad);
        while (read == 5) begin
            while (!rx_payload_valid) @(negedge clk);
            repeat (3) begin
                if ({rx_payload_data,rx_payload_keep,rx_payload_first,rx_payload_last} !==
                    {data,keep,first,last})
                    $fatal(1,"RX payload mismatch");
                @(negedge clk);
            end
            rx_payload_ready = 1;
            @(negedge clk);
            rx_payload_ready = 0;
            read = $fscanf(fd,"%h %h %b %b %b\n",data,keep,first,last,bad);
        end
        if (read != -1) $fatal(1,"bad expected UDP payload row");
        $fclose(fd);
    endtask

    task automatic snapshot_check(input logic [4:0] index, input logic [31:0] value);
        status_index = index;
        #1;
        if (status_read_data !== value)
            $fatal(1,"snapshot row %0d got %0d expected %0d",index,status_read_data,value);
    endtask

    task automatic drain_and_report(
        input integer lane,
        input string name,
        input logic allow_group, fence,
        input logic [31:0] cookie,
        input logic [7:0] status,
        input logic [31:0] hop,
        input logic [ROW_W-1:0] row
    );
        descriptor(lane,name,allow_group,fence);
        body(lane,name);
        report(1'(lane),cookie,status,0,hop,row,1);
    endtask

    initial begin
        string trace_name, wave;
        logic [63:0] stamp;
        if (!$value$plusargs("vectors=%s",directory) || !$value$plusargs("capture=%s",trace_name))
            $fatal(1,"missing test files");
        if ($value$plusargs("wave=%s",wave)) begin
            $dumpfile(wave);
            $dumpvars(0,tb_eth_routed_udp_frontend);
        end
        trace_fd = $fopen(trace_name,"w");
        if (!trace_fd) $fatal(1,"could not open MAC capture");
        repeat (2) @(negedge clk);
        rst = 0;
        load_routes();
        commit_routes();
        load_routes();
        rule_enable();

        mac_pause = 1;
        fork
            descriptor(0,"routed",0,0);
            descriptor(1,"group",1,0);
        join
        while (!mac_tx_valid) @(negedge clk);
        repeat (8) @(negedge clk);
        mac_pause = 0;
        fork
            body(0,"routed");
            body(1,"group");
            begin
                report(1,'h100,0,61,GROUP,'1,1);
                if (arp_frames != 1) $fatal(1,"known multicast did not bypass pending ARP");
                send_frame("wrong_vlan",stamp);
                repeat (10) @(negedge clk);
                if (tx_payload_ready[0] || tx_status_valid)
                    $fatal(1,"wrong VLAN resolved the first request");
                send_frame("gateway_24",stamp);
                report(0,'h100,0,62,PEER,1,1);
            end
        join

        descriptor(0,"cached",0,1);
        body(0,"cached");
        report(0,'h101,0,60,PEER,1,1);
        if (arp_frames != 1) $fatal(1,"neighbor cache missed the repeated VLAN lookup");

        descriptor(0,"epoch",0,1);
        while (arp_frames < 2) @(negedge clk);
        repeat (4) commit_routes();
        if (active_epoch != 1) $fatal(1,"did not wrap route epoch");
        fork
            body(0,"epoch");
            send_frame("gateway_25",stamp);
        join
        report(0,'h102,40,0,OTHER_PEER,3,1);

        drain_and_report(0,"blocked",0,1,'h103,34,0,2);
        drain_and_report(1,"group_denied",0,0,'h104,39,0,'1);
        drain_and_report(1,"miss",0,0,'h105,33,0,'1);
        drain_and_report(1,"bad_vid",1,0,'h106,35,0,'1);
        drain_and_report(1,"bad_shape",1,0,'h108,17,0,'1);
        drain_and_report(1,"bad_body",1,0,'h109,18,GROUP,'1);

        send_frame("peer_probe",stamp);
        send_frame("ipv4",stamp);
        receive_udp(0,stamp);
        read_body();
        send_frame("bad_ipv4",stamp);
        receive_udp(2,stamp);
        send_frame("other",stamp);
        send_frame("short",stamp);
        send_frame("stacked_arp",stamp);
        send_frame("triple_vlan",stamp);
        drain_and_report(0,"timeout",0,1,'h107,36,UNREACHABLE,0);

        repeat (20) @(negedge clk);
        if (rx_info_valid || rx_payload_valid || tx_status_valid || mac_tx_valid)
            $fatal(1,"unexpected outputs after all frames");
        status_snapshot = 1;
        @(negedge clk);
        status_snapshot = 0;
        if (status_snapshot_id != 1) $fatal(1,"snapshot lost");
        snapshot_check(0,0);
        snapshot_check(1,5);
        snapshot_check(2,5);
        snapshot_check(3,2);
        snapshot_check(4,4);
        snapshot_check(5,2);
        snapshot_check(6,2);
        snapshot_check(7,1);
        snapshot_check(8,3);
        snapshot_check(10,4);
        snapshot_check(11,2);
        snapshot_check(12,1);
        snapshot_check(13,1);
        snapshot_check(16,1);
        snapshot_check(17,0);
        snapshot_check(18,1);
        snapshot_check(19,1);
        snapshot_check(20,5);
        snapshot_check(21,2);
        snapshot_check(22,1);
        snapshot_check(31,0);
        if (physical_frames != 8 || reports != 11 || mac_stalls < 24 || report_stalls < 40)
            $fatal(1,"coverage mismatch: frames=%0d reports=%0d stalls=%0d/%0d",
                   physical_frames,reports,mac_stalls,report_stalls);
        $fclose(trace_fd);
        $display("PASS routed endpoint: frames=%0d reports=%0d held cycles MAC=%0d status=%0d",
                 physical_frames,reports,mac_stalls,report_stalls);
        $finish;
    end

    initial begin
        #100000;
        $fatal(1,"routed integration timeout cycle=%0d frames=%0d reports=%0d",
               cycle,physical_frames,reports);
    end
endmodule

`default_nettype wire
