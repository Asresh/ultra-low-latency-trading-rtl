`timescale 1ns/1ps
`default_nettype none

// Expected events are FIFO-ordered independently for each session. No fixed
// cross-session order is imposed while the physical lanes run concurrently.
module tb_multisession_market_data;
    localparam integer SLOTS = 3, EPOCH_W = 3, WAIT_W = 8;
    localparam integer SLOT_W = 2, KEY_W = 2, EXPECT_DEPTH = 4096;
    localparam logic [79:0] ALPHA = "ALPHA00001", BETA = "BETA000002",
                           GAMMA = "GAMMA00003", RENAMED = "RENAME0004";
    localparam logic [63:0] BETA_START = 64'hffff_ffff_ffff_fffe;

    logic clk = 0, rst = 1;
    always #5 clk = !clk;
    logic cfg_valid = 0, cfg_enable = 0, cfg_force = 0, ack_ready = 0;
    wire cfg_ready, ack_valid, ack_active;
    logic [KEY_W-1:0] cfg_slot = '0;
    logic [EPOCH_W-1:0] cfg_expected_epoch = '0;
    logic [79:0] cfg_session = '0;
    logic [63:0] cfg_sequence = '0;
    logic [WAIT_W-1:0] cfg_request_spacing = '0;
    wire [1:0] ack_status, ack_cut_lanes;
    wire [KEY_W-1:0] ack_slot;
    wire [EPOCH_W-1:0] ack_epoch;

    logic [1:0] feed_valid = '0, feed_first = '0, feed_last = '0;
    wire [1:0] feed_ready;
    logic [127:0] feed_data = '0;
    logic [15:0] feed_keep = '0;
    logic [2*KEY_W-1:0] feed_slots = '0;
    logic event_ready = 1, request_ready = 1;
    wire event_valid, request_valid;
    wire [SLOT_W-1:0] event_slot, request_slot;
    wire [EPOCH_W-1:0] event_epoch, request_epoch;
    wire [587:0] event_data;
    wire [159:0] request_data;
    logic [KEY_W-1:0] monitor_slot = 0;
    logic monitor_snapshot = 0;
    logic [3:0] monitor_index = 0;
    wire monitor_valid, monitor_active, monitor_fenced, monitor_gap, monitor_end;
    wire [EPOCH_W-1:0] monitor_epoch;
    wire [79:0] monitor_session;
    wire [63:0] monitor_expected, monitor_high_water, monitor_packets, discarded_packets;
    wire [5:0] monitor_occupancy, monitor_peak;
    wire [31:0] monitor_statistic, monitor_events, monitor_requests;
    wire [WAIT_W-1:0] monitor_cooldown;

    multisession_market_data #(
        .SLOTS(SLOTS), .EPOCH_W(EPOCH_W), .WAIT_W(WAIT_W),
        .PACKET_BYTES(128), .BLOCKS(8), .WINDOW(8), .GAP_TICKS(20)
    ) dut (.*);

    integer accepted_events = 0, accepted_requests = 0, accepted_acks = 0;
    integer accepted_beats = 0, held_events = 0, held_requests = 0;
    integer event_written [0:SLOTS-1], event_read [0:SLOTS-1];
    logic [EPOCH_W+587:0] wanted [0:SLOTS-1][0:EXPECT_DEPTH-1];
    logic [79:0] configured_session [0:SLOTS-1];
    logic [EPOCH_W-1:0] configured_epoch [0:SLOTS-1];
    integer spacing [0:SLOTS-1], last_request [0:SLOTS-1];
    integer cycle = 0;
    logic prior_event_hold = 0, prior_request_hold = 0, prior_ack_hold = 0;
    logic [SLOT_W+EPOCH_W+587:0] prior_event;
    logic [SLOT_W+EPOCH_W+159:0] prior_request;
    logic [2+KEY_W+EPOCH_W+3-1:0] prior_ack;
    logic [159:0] last_payload [0:SLOTS-1];
    string vector_file, waveform_file, request_file, repair_file;
    logic [63:0] recorded_data [0:1][0:511];
    logic [7:0] recorded_keep [0:1][0:511];
    integer recorded_slot [0:1][0:511];
    logic [1:0] recorded_framing [0:1][0:511];
    integer recorded_length [0:1];

    always @(posedge clk) begin
        cycle = cycle + 1;
        if (rst) begin
            prior_event_hold = 0;
            prior_request_hold = 0;
            prior_ack_hold = 0;
        end else begin
            if (prior_event_hold && (!event_valid ||
                {event_slot,event_epoch,event_data} !== prior_event))
                $fatal(1, "held event changed");
            if (prior_request_hold && (!request_valid ||
                {request_slot,request_epoch,request_data} !== prior_request))
                $fatal(1, "held request changed");
            if (prior_ack_hold && (!ack_valid ||
                {ack_status,ack_slot,ack_epoch,ack_active,ack_cut_lanes} !== prior_ack))
                $fatal(1, "held acknowledgement changed");
            prior_event_hold = event_valid && !event_ready;
            prior_request_hold = request_valid && !request_ready;
            prior_ack_hold = ack_valid && !ack_ready;
            prior_event = {event_slot,event_epoch,event_data};
            prior_request = {request_slot,request_epoch,request_data};
            prior_ack = {ack_status,ack_slot,ack_epoch,ack_active,ack_cut_lanes};
            accepted_beats = accepted_beats + $countones(feed_valid & feed_ready);
            if (prior_event_hold)
                held_events++;
            if (prior_request_hold)
                held_requests++;
            if (event_valid && event_ready) begin
                if (int'(event_slot) >= SLOTS)
                    $fatal(1, "out-of-range event slot");
                if (event_read[event_slot] == event_written[event_slot])
                    $fatal(1, "unexpected slot %d epoch %d sequence %h",
                           event_slot,event_epoch,event_data[583:520]);
                if ({event_epoch,event_data} !== wanted[event_slot][event_read[event_slot]])
                    $fatal(1, "slot %d event index %d got epoch %d sequence %h expected %h",
                           event_slot,event_read[event_slot],event_epoch,event_data[583:520],
                           wanted[event_slot][event_read[event_slot]]);
                event_read[event_slot]++;
                accepted_events++;
            end
            if (request_valid && request_ready) begin
                if (int'(request_slot) >= SLOTS ||
                    request_data[159:80] !== configured_session[request_slot] ||
                    request_epoch !== configured_epoch[request_slot] ||
                    request_data[15:0] == 0 || request_data[15:0] > 8)
                    $fatal(1, "request crossed a session or epoch boundary");
                if (cycle - last_request[request_slot] <= spacing[request_slot])
                    $fatal(1, "request spacing violated");
                last_request[request_slot] = cycle;
                last_payload[request_slot] = request_data;
                accepted_requests++;
            end
            if (ack_valid && ack_ready)
                accepted_acks++;
        end
    end

    // Write one wire beat on one source lane and retain it until transfer.
    task automatic transmit(
        input integer lane, slot, input logic [63:0] word,
        input logic [7:0] keep, input bit first, last
    );
        begin
            @(negedge clk);
            feed_slots[lane*KEY_W+:KEY_W] = KEY_W'(slot);
            feed_data[lane*64+:64] = word;
            feed_keep[lane*8+:8] = keep;
            feed_first[lane] = first;
            feed_last[lane] = last;
            feed_valid[lane] = 1;
            do @(posedge clk); while (!feed_ready[lane]);
            @(negedge clk);
            feed_valid[lane] = 0;
        end
    endtask

    // Build a 41-byte MoldUDP64 datagram with one 19-byte Delete message.
    function automatic logic [327:0] delete_packet(
        input logic [79:0] session, input logic [63:0] seq_number, order_ref
    );
        logic [327:0] packet;
        begin
            packet = '0;
            for (integer index = 0; index < 10; index++)
                packet[index*8+:8] = session[(9-index)*8+:8];
            for (integer index = 0; index < 8; index++) begin
                packet[(10+index)*8+:8] = seq_number[(7-index)*8+:8];
                packet[(33+index)*8+:8] = order_ref[(7-index)*8+:8];
            end
            packet[19*8+:8] = 8'd1;
            packet[21*8+:8] = 8'd19;
            packet[22*8+:8] = "D";
            return packet;
        end
    endfunction

    // Subsequent beats deliberately carry a different logical slot field.
    task automatic send_delete(
        input integer lane, slot, input logic [79:0] session,
        input logic [63:0] seq_number, order_ref, input integer start, stop
    );
        logic [383:0] frame;
        begin
            frame = 384'(delete_packet(session,seq_number,order_ref));
            for (integer beat = start; beat < stop; beat++)
                transmit(lane, beat == 0 ? slot : (slot+1)%SLOTS,
                         frame[beat*64+:64],beat == 5 ? 8'h01 : 8'hff,
                         beat == 0,beat == 5);
        end
    endtask

    task automatic expect_event(
        input integer index, input logic [EPOCH_W-1:0] epoch,
        input logic [587:0] data
    );
        begin
            if (event_written[index] == EXPECT_DEPTH)
                $fatal(1, "expected-event memory exhausted");
            wanted[index][event_written[index]] = {epoch,data};
            event_written[index]++;
        end
    endtask

    task automatic expect_delete(
        input integer index, input logic [EPOCH_W-1:0] epoch,
        input logic [63:0] seq_number, order_ref
    );
        logic [587:0] data;
        begin
            data = '0;
            data[583:520] = seq_number;
            data[519:456] = seq_number;
            data[455:448] = "D";
            data[367:304] = order_ref;
            expect_event(index,epoch,data);
        end
    endtask

    // Compare-and-swap a context without waiting for its eventual response.
    task automatic submit_config(
        input integer slot, epoch, input bit enable, force_cut,
        input logic [79:0] session, input logic [63:0] seq_number, input integer pace
    );
        begin
            @(negedge clk);
            cfg_slot = KEY_W'(slot);
            cfg_expected_epoch = EPOCH_W'(epoch);
            cfg_enable = enable;
            cfg_force = force_cut;
            cfg_session = session;
            cfg_sequence = seq_number;
            cfg_request_spacing = WAIT_W'(pace);
            cfg_valid = 1;
            do @(posedge clk); while (!cfg_ready);
            @(negedge clk);
            cfg_valid = 0;
        end
    endtask

    task automatic consume_ack(
        input integer slot, status, epoch, input bit active,
        input logic [1:0] cut, input integer stalls
    );
        begin
            while (!ack_valid)
                @(negedge clk);
            repeat (stalls)
                @(negedge clk);
            if ({ack_slot,ack_status,ack_epoch,ack_active,ack_cut_lanes} !==
                {KEY_W'(slot),2'(status),EPOCH_W'(epoch),active,cut})
                $fatal(1, "unexpected config ack slot=%d status=%d epoch=%d active=%b cut=%b",
                       ack_slot,ack_status,ack_epoch,ack_active,ack_cut_lanes);
            ack_ready = 1;
            @(negedge clk);
            ack_ready = 0;
        end
    endtask

    task automatic remember_config(
        input integer slot, epoch, input logic [79:0] session, input integer pace
    );
        begin
            configured_session[slot] = session;
            configured_epoch[slot] = EPOCH_W'(epoch);
            last_request[slot] = -100000;
            spacing[slot] = pace;
        end
    endtask

    task automatic initial_config(
        input integer slot, input logic [79:0] session,
        input logic [63:0] seq_number, input integer pace
    );
        begin
            submit_config(slot,0,1,0,session,seq_number,pace);
            consume_ack(slot,0,1,1,0,5);
            remember_config(slot,1,session,pace);
        end
    endtask

    task automatic wait_all_expected;
        begin
            for (integer index = 0; index < SLOTS; index++)
                while (event_read[index] != event_written[index])
                    @(negedge clk);
        end
    endtask

    task automatic route_and_fence;
        integer tally;
        begin
            expect_delete(0,1,100,64'habcdfd00);
            fork
                send_delete(0,0,ALPHA,100,64'habcdfd00,0,6);
                send_delete(1,0,ALPHA,100,64'habcdfd00,0,6);
            join
            wait_all_expected();
            repeat (50) @(negedge clk);
            monitor_snapshot = 1;
            @(negedge clk);
            monitor_snapshot = 0;
            if (monitor_packets !== 64'h0000000100000001 ||
                monitor_events != 1 || monitor_expected != 101 || monitor_requests != 0)
                $fatal(1, "slot 0 snapshot differs from accepted traffic");

            event_ready = 0;
            expect_delete(0,1,101,64'habcdfd01);
            send_delete(0,0,ALPHA,101,64'habcdfd01,0,6);
            while (!event_valid) @(negedge clk);
            if (event_slot != 0 || event_epoch != 1)
                $fatal(1, "wrong event captured before reconfiguration");
            submit_config(0,1,1,0,RENAMED,900,40);
            expect_delete(1,1,BETA_START,64'hab000021);
            send_delete(1,1,BETA,BETA_START,64'hab000021,0,6);
            repeat (24) @(negedge clk);
            if (ack_valid || !monitor_fenced || monitor_epoch != 1)
                $fatal(1, "configuration did not wait for exported event");
            event_ready = 1;
            consume_ack(0,0,2,1,0,5);
            remember_config(0,2,RENAMED,40);
            expect_delete(0,2,900,64'hab000030);
            send_delete(0,0,RENAMED,900,64'hab000030,0,6);
            wait_all_expected();

            // An unforced replacement waits for an accepted packet to end.
            expect_delete(1,1,BETA_START+1,64'hab000041);
            expect_delete(1,1,0,64'hab000042);
            send_delete(0,1,BETA,BETA_START+1,64'hab000041,0,6);
            send_delete(1,1,BETA,0,64'hab000042,0,6);
            wait_all_expected();
            send_delete(0,1,BETA,1,64'hab000044,0,2);
            monitor_slot = 1;
            submit_config(1,1,1,0,BETA,0,32);
            expect_delete(2,1,500,64'hab000050);
            send_delete(1,2,GAMMA,500,64'hab000050,0,6);
            repeat (30) @(negedge clk);
            if (ack_valid || !monitor_fenced || event_read[2] != event_written[2])
                $fatal(1, "ordinary fence blocked unrelated context or ignored existing lease");
            send_delete(0,1,BETA,1,64'hab000044,2,6);
            consume_ack(1,0,2,1,0,1);
            remember_config(1,2,BETA,32);

            // A forced replacement quarantines an unfinished tail even after ack.
            tally = int'(discarded_packets[63:32]);
            send_delete(1,2,GAMMA,501,64'habad1234,0,1);
            submit_config(2,1,1,1,GAMMA,501,30);
            consume_ack(2,0,2,1,2'b10,3);
            remember_config(2,2,GAMMA,30);
            send_delete(1,2,GAMMA,501,64'habad1234,1,6);
            if (int'(discarded_packets[63:32]) != tally+1)
                $fatal(1, "forced tail did not complete as one discarded packet");
            expect_delete(2,2,501,64'habad4321);
            send_delete(1,2,GAMMA,501,64'habad4321,0,6);
            wait_all_expected();
            submit_config(0,1,0,0,ALPHA,0,0);
            consume_ack(0,2,2,1,0,3);
            submit_config(3,0,0,0,ALPHA,0,0);
            consume_ack(3,1,0,0,0,3);
            transmit(0,3,64'heffefeff,8'h0f,1,1);
            if (discarded_packets[31:0] !== 32'd1)
                $fatal(1, "unroutable packet was not counted on lane A");
        end
    endtask

    task automatic recovery_and_retry;
        integer prior, file;
        begin
            request_ready = 0;
            expect_delete(0,2,901,64'ha901);
            expect_delete(0,2,902,64'ha902);
            expect_delete(0,2,903,64'ha903);
            expect_delete(1,2,0,64'hb000);
            expect_delete(1,2,1,64'hb001);
            expect_delete(1,2,2,64'hb002);
            fork
                send_delete(0,0,RENAMED,903,64'ha903,0,6);
                send_delete(1,1,BETA,2,64'hb002,0,6);
            join
            while (!request_valid) @(negedge clk);
            repeat (48) @(negedge clk);
            prior = accepted_requests;
            request_ready = 1;
            while (accepted_requests < prior+6) @(negedge clk);
            if (last_payload[0] !== {RENAMED,64'd901,16'd2} ||
                last_payload[1] !== {BETA,64'd0,16'd2})
                $fatal(1, "merged retries do not identify the two independent missing spans");
            if ($value$plusargs("requests=%s",request_file)) begin
                file = $fopen(request_file,"w");
                if (file == 0)
                    $fatal(1, "cannot open request capture");
                for (integer index = 0; index < 2; index++)
                    $fdisplay(file,"%0d %0d %040h",index,configured_epoch[index],last_payload[index]);
                $fclose(file);
            end
            if ($value$plusargs("repair=%s",repair_file)) begin
                file = $fopen(repair_file,"r");
                if (file == 0)
                    $fatal(1, "cannot open archive-generated repair bytes");
                read_lanes(file);
                $fclose(file);
                fork
                    send_recorded(0,0);
                    send_recorded(1,0);
                join
            end else begin
                fork
                    begin
                        send_delete(0,0,RENAMED,901,64'ha901,0,6);
                        send_delete(0,0,RENAMED,902,64'ha902,0,6);
                    end
                    begin
                        send_delete(1,1,BETA,0,64'hb000,0,6);
                        send_delete(1,1,BETA,1,64'hb001,0,6);
                    end
                join
            end
            wait_all_expected();
            repeat (80) @(negedge clk);
            prior = accepted_requests;
            repeat (80) @(negedge clk);
            if (accepted_requests != prior)
                $fatal(1, "recovery retries continued after both gaps closed");
        end
    endtask

    // The same file is read sequentially before launching the two lane drivers.
    // Each lane has independent pacing, while the next phase waits for all events.
    task automatic send_recorded(input integer lane, phase);
        integer random_state;
        begin
            random_state = 17 + 31*phase + 79*lane;
            for (integer i = 0; i < recorded_length[lane]; i++) begin
                random_state = (random_state * 1103515245 + 12345) & 32'h7fffffff;
                repeat ((random_state >> 13) & 3) @(negedge clk);
                transmit(lane, recorded_slot[lane][i], recorded_data[lane][i],
                         recorded_keep[lane][i], recorded_framing[lane][i][1],
                         recorded_framing[lane][i][0]);
            end
        end
    endtask

    task automatic read_lanes(input integer file);
        integer fields, slot, count;
        logic [63:0] data_word;
        logic [7:0] keep_word;
        logic [1:0] framing;
        begin
            for (integer lane = 0; lane < 2; lane++) begin
                fields = $fscanf(file,"%d",count);
                if (fields != 1 || count < 0 || count > 512)
                    $fatal(1, "malformed lane length");
                recorded_length[lane] = count;
                for (integer index = 0; index < recorded_length[lane]; index++) begin
                    fields = $fscanf(file,"%d %h %h %h",slot,data_word,keep_word,framing);
                    if (fields != 4)
                        $fatal(1, "malformed recorded beat");
                    recorded_slot[lane][index] = slot;
                    recorded_data[lane][index] = data_word;
                    recorded_keep[lane][index] = keep_word;
                    recorded_framing[lane][index] = framing;
                end
            end
        end
    endtask

    task automatic replay_vectors;
        integer file, phases, fields, slot, count;
        logic [587:0] value;
        begin
            if (!$value$plusargs("vectors=%s",vector_file))
                $fatal(1, "provide +vectors for the independent multi-session scoreboard");
            file = $fopen(vector_file,"r");
            if (file == 0 || $fscanf(file,"%d",phases) != 1 || phases <= 0)
                $fatal(1, "cannot read vector phases");
            for (integer phase = 0; phase < phases; phase++) begin
                fields = $fscanf(file,"%d",count);
                if (fields != 1 || count < 1)
                    $fatal(1, "test phase has no expected events");
                repeat (count) begin
                    fields = $fscanf(file,"%d %h",slot,value);
                    if (fields != 2 || slot < 0 || slot >= SLOTS)
                        $fatal(1, "malformed event fixture");
                    expect_event(slot,configured_epoch[slot],value);
                end
                read_lanes(file);
                fork
                    send_recorded(0,phase);
                    send_recorded(1,phase);
                join
                wait_all_expected();
                repeat (24) @(negedge clk);
            end
            $fclose(file);
        end
    endtask

    initial begin
        if ($value$plusargs("waveform=%s",waveform_file)) begin
            $dumpfile(waveform_file);
            $dumpvars(1,tb_multisession_market_data);
        end
        for (integer i = 0; i < SLOTS; i++) begin
            event_written[i] = 0;
            event_read[i] = 0;
            last_request[i] = -100000;
        end
        repeat (4) @(negedge clk);
        rst = 0;
        initial_config(0,ALPHA,100,40);
        initial_config(1,BETA,BETA_START,32);
        initial_config(2,GAMMA,500,30);
        route_and_fence();
        recovery_and_retry();
        replay_vectors();

        for (integer previous = 2; previous < 7; previous++) begin
            submit_config(2,previous,0,0,GAMMA,0,0);
            consume_ack(2,0,previous+1,0,0,1);
        end
        submit_config(2,7,1,0,GAMMA,0,0);
        consume_ack(2,3,7,0,0,1);
        monitor_slot = 2;
        #1;
        if (monitor_active || monitor_epoch != 7)
            $fatal(1, "epoch exhaustion changed disabled slot");
        monitor_slot = 3;
        #1;
        if (monitor_valid || monitor_packets != 0 || monitor_fenced)
            $fatal(1, "invalid monitor key exposes activity");
        if (held_events < 20 || held_requests < 40 || accepted_requests < 6 ||
            accepted_events < 100 || accepted_acks != 14)
            $fatal(1, "multisession coverage incomplete");
        $display("PASS multisession: %0d events, %0d retries, %0d config responses, %0d ingress beats",
                 accepted_events,accepted_requests,accepted_acks,accepted_beats);
        $display("Held event edges=%0d, held request edges=%0d, discarded A/B=%0d/%0d",
                 held_events,held_requests,discarded_packets[31:0],discarded_packets[63:32]);
        $finish;
    end
    initial begin #8000000; $fatal(1, "multisession regression timed out"); end
endmodule

`default_nettype wire
