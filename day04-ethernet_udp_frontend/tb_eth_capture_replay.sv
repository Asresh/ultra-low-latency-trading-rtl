`timescale 1ns/1ps

module tb_eth_capture_replay;
    logic clk=0;
    always #5 clk=!clk;
    logic rst=1;
    logic [63:0] timestamp=0;

    logic mac_valid=0, mac_ready;
    logic [63:0] mac_data=0;
    logic [7:0] mac_keep=0;
    logic mac_first=0, mac_last=0, mac_bad=0;
    logic rule_write_valid=0, rule_write_ready;
    logic [2:0] rule_write_index=0;
    logic [255:0] rule_write_data=0;
    logic rule_commit_valid=0, rule_commit_ready;
    logic [7:0] rule_epoch;
    logic info_valid, info_ready=0;
    logic [383:0] info_data;
    logic payload_valid, payload_ready=0;
    logic [63:0] payload_data;
    logic [7:0] payload_keep;
    logic payload_first, payload_last;

    `ifdef STRICT_UDP
    localparam bit PERMIT_ZERO=0;
    `else
    localparam bit PERMIT_ZERO=1;
    `endif

    eth_rx_path #(.ALLOW_ZERO_UDP(PERMIT_ZERO)) dut (.*);

    integer packet_serial [0:16383];
    logic [63:0] packet_sof [0:16383];
    integer frames=0, output_frames=0, body_beats=0, wire_beats=0;
    integer wire_count, info_count, payload_count;
    integer mac_file, info_file, payload_file, config_file, counts_file;
    integer serial_no, requested, first_flag, last_flag, bad_flag, keep_count;
    integer source_rc, output_serial, body_serial, consumer_epoch;
    integer stored_bytes=0, input_delay_cycles=0, late_packets=0, rejects=0;
    integer scheduled_start=0, remaining=0, oracle_rc, body_rc;
    integer finish_time, total_cycle=0;
    logic [63:0] wire_value, oracle_beat;
    logic [7:0] wire_keep, oracle_keep;
    logic oracle_first, oracle_last, oracle_bad;
    logic [383:0] oracle_metadata, known_mask;
    logic [31:0] ready_state=32'h1579dea2;
    logic open_rx=0, info_stall=0, body_stall=0;
    logic [383:0] held_metadata;
    logic [73:0] held_body;
    string directory;

    always @(posedge clk) begin
        total_cycle=total_cycle+1;
        if (rst)
            timestamp <= 64'd0;
        else
            timestamp <= timestamp+64'd1;
        if (total_cycle > 5000000)
            $fatal(1,"capture replay exceeded cycle budget");
    end

    // Advance the seeded generator by one step for reproducible test patterns.
    function automatic logic [31:0] advance(input logic [31:0] x);
        logic [31:0] y;
        begin
            y=x^(x<<13);
            y=y^(y>>17);
            advance=y^(y<<5);
        end
    endfunction

    always @(negedge clk) begin
        ready_state=advance(ready_state);
        info_ready=ready_state[2:0]>=3;
        payload_ready=ready_state[7:5]>=4;
    end

    always @(posedge clk) if (!rst) begin
        if (info_stall && (!info_valid || held_metadata !== info_data))
            $fatal(1,"capture metadata was unstable through backpressure");
        if (body_stall && (!payload_valid ||
            held_body !== {payload_first,payload_last,payload_keep,payload_data}))
            $fatal(1,"capture body was unstable through backpressure");
        info_stall=info_valid && !info_ready;
        body_stall=payload_valid && !payload_ready;
        if (info_stall)
            held_metadata=info_data;
        if (body_stall)
            held_body={payload_first,payload_last,payload_keep,payload_data};

        if (mac_valid && !mac_ready)
            input_delay_cycles=input_delay_cycles+1;
        if (mac_valid && mac_ready) begin
            wire_beats=wire_beats+1;
            if (mac_first) begin
                if (open_rx)
                    $fatal(1,"input capture reordered bytes");
                packet_serial[frames]=serial_no;
                packet_sof[frames]=timestamp;
                if (int'(timestamp)-scheduled_start > requested)
                    late_packets=late_packets+1;
            end
            open_rx=!mac_last;
            if (mac_last)
                frames=frames+1;
        end
        if (info_valid && info_ready) begin
            if (output_frames >= frames || remaining != 0)
                $fatal(1,"capture RX released ahead of full input packet or older payload");
            oracle_rc=$fscanf(info_file,"%d %h %h",output_serial,oracle_metadata,known_mask);
            if (oracle_rc != 3 || output_serial != packet_serial[output_frames] ||
                oracle_metadata !== (known_mask & info_data) ||
                info_data[351:288] !== packet_sof[output_frames])
                $fatal(1,"capture packet number %0d metadata mismatch\n%h\n%h",
                       output_frames, info_data, oracle_metadata);
            output_frames=output_frames+1;
            if (info_data[383:376] == 0) begin
                remaining=int'(info_data[95:80]);
            end else begin
                rejects=rejects+1;
            end
        end
        if (payload_valid && payload_ready) begin
            if (remaining == 0)
                $fatal(1,"payload with no prior accepted metadata");
            body_rc=$fscanf(payload_file,"%d %h %h %d %d %d",body_serial,oracle_beat,oracle_keep,
                            oracle_first,oracle_last,oracle_bad);
            if (body_rc != 6 || body_serial != packet_serial[output_frames-1] ||
                {payload_data,payload_keep,payload_first,payload_last} !==
                {oracle_beat,oracle_keep,oracle_first,oracle_last})
                $fatal(1,"capture packet %0d body beat mismatch",packet_serial[output_frames-1]);
            keep_count=0;
            for (integer lane=0; lane<8; lane=lane+1)
                if (payload_keep[lane])
                    keep_count=keep_count+1;
            remaining=remaining-keep_count;
            if (payload_last != (remaining == 0))
                $fatal(1,"capture payload count disagrees with delivered metadata");
            stored_bytes=stored_bytes+keep_count;
            body_beats=body_beats+1;
        end
    end

    // Load the capture replay rule image before accepting frames.
    task automatic load_rules;
        logic [255:0] raw_rule;
        integer rc;
        begin
            for (integer i=0; i<8; i=i+1) begin
                rc=$fscanf(config_file,"%h",raw_rule);
                if (rc != 1)
                    $fatal(1,"capture bundle must contain eight rule rows");
                @(negedge clk);
                rule_write_index=3'(i);
                rule_write_data=raw_rule;
                rule_write_valid=1;
                do @(posedge clk); while (!rule_write_ready);
            end
            @(negedge clk);
            rule_write_valid=0;
            rule_commit_valid=1;
            do @(posedge clk); while (!rule_commit_ready);
            @(negedge clk);
            rule_commit_valid=0;
            consumer_epoch=int'(rule_epoch);
            if (consumer_epoch != 1)
                $fatal(1,"capture classification expected epoch one");
        end
    endtask

    // Read a fixture beat and hold it on the stream until the DUT accepts it.
    task automatic send_row;
        begin
            @(negedge clk);
            if (int'(timestamp)-scheduled_start < requested) begin
                mac_valid=0;
                while (int'(timestamp)-scheduled_start < requested)
                    @(negedge clk);
            end
            mac_valid=1;
            mac_data=wire_value;
            mac_keep=wire_keep;
            mac_first=first_flag != 0;
            mac_last=last_flag != 0;
            mac_bad=bad_flag != 0;
            do @(posedge clk); while (!mac_ready);
        end
    endtask

    initial begin
        if (!$value$plusargs("vectors=%s",directory))
            directory="build/replay";
        config_file=$fopen({directory,"/rt_rules.txt"},"r");
        counts_file=$fopen({directory,"/rt_counts.txt"},"r");
        mac_file=$fopen({directory,"/rt_mac.txt"},"r");
        info_file=$fopen({directory,"/rt_info.txt"},"r");
        payload_file=$fopen({directory,"/rt_payload.txt"},"r");
        if (!config_file || !counts_file || !mac_file || !info_file || !payload_file)
            $fatal(1,"could not open all replay files in %s",directory);
        if ($fscanf(counts_file,"%d %d %d",wire_count,info_count,payload_count) != 3 ||
            info_count > 16384 || info_count < 1)
            $fatal(1,"bad or empty replay geometry");

        repeat (4) @(negedge clk);
        rst=0;
        load_rules();
        scheduled_start=int'(timestamp)+1;
        while (!$feof(mac_file)) begin
            source_rc=$fscanf(mac_file,"%d %d %h %h %d %d %d",requested,serial_no,
                              wire_value,wire_keep,first_flag,last_flag,bad_flag);
            if (source_rc == 7)
                send_row();
            else if (!$feof(mac_file))
                $fatal(1,"bad capture stream row after %0d beats",wire_beats);
        end
        @(negedge clk);
        mac_valid=0;
        finish_time=total_cycle+10000;
        while (output_frames != info_count || body_beats != payload_count) begin
            @(negedge clk);
            if (total_cycle > finish_time)
                $fatal(1,"capture replay failed to drain");
        end
        if (wire_beats != wire_count || output_frames != frames || remaining != 0 ||
            $fscanf(info_file,"%d",output_serial) == 1 || $fscanf(payload_file,"%d",body_serial) == 1)
            $fatal(1,"capture replay count mismatch");

        $display("PASS capture replay: %0d packets, %0d rejections, %0d payload bytes; %0d late first beats, %0d held input cycles",
                 frames,rejects,stored_bytes,late_packets,input_delay_cycles);
        $finish;
    end
endmodule
