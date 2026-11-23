`timescale 1ns/1ps
`default_nettype none

module ouch_order_session #(
    parameter integer ORDER_SLOTS = 8,
    parameter integer TX_HEARTBEAT_TICKS = 1_000,
    parameter integer RX_TIMEOUT_TICKS = 5_000
) (
    input wire clk,
    input wire rst,
    input wire transport_up,
    input wire time_tick,
    input wire kill_req,
    output wire kill_latched,
    output wire close_transport,
    input wire s_login_valid,
    output logic s_login_ready,
    input ouch_wire_pkg::login_t s_login,
    input wire s_logout_valid,
    output logic s_logout_ready,
    input wire s_cmd_valid,
    output logic s_cmd_ready,
    input ouch_wire_pkg::command_t s_cmd,
    input wire s_rx_valid,
    output wire s_rx_ready,
    input wire [63:0] s_rx_data,
    input wire [7:0] s_rx_keep,
    output wire m_tx_valid,
    input wire m_tx_ready,
    output wire [63:0] m_tx_data,
    output wire [7:0] m_tx_keep,
    output wire m_tx_last,
    output wire m_notice_valid,
    input wire m_notice_ready,
    output ouch_wire_pkg::notice_t m_notice,
    input wire [7:0] query_slot,
    output ouch_wire_pkg::slot_view_t query_view,
    output ouch_wire_pkg::metrics_t metrics,
    output ouch_wire_pkg::session_state_t session_state,
    output ouch_wire_pkg::status_t last_fault,
    output wire [79:0] session_id,
    output wire [63:0] next_sequence,
    output wire replaying,
    output wire market_open,
    output wire [31:0] highest_order_ref,
    output wire [7:0] live_count,
    output wire [7:0] pending_count
);
    import ouch_wire_pkg::*;

    session_state_t state_q, state_d;
    status_t fault_q, parser_fault, urgent_fault;
    login_t login_q;
    logic [79:0] session_q, accepted_session;
    logic [63:0] checkpoint, socket_cursor;
    logic has_checkpoint;
    logic market_q, kill_q, logout_pending, logout_complete;
    logic notice_valid_q;
    notice_t notice_q, notice_d;
    logic notice_write;
    metrics_t metrics_q;
    logic [31:0] rx_idle, tx_idle;

    logic packet_ready, tx_submit;
    wire packet_valid, tx_ready;
    packet_t packet, tx_packet, command_packet;
    inbound_t inbound;
    notice_t order_notice, local_notice;
    logic cmd_commit, order_commit;
    wire cmd_sends;
    status_t local_status, admission_status;
    wire transport_flush = rst || !transport_up;
    wire space = !notice_valid_q || m_notice_ready;
    wire tx_fire = m_tx_valid && m_tx_ready;
    wire login_port_idle = state_q == S_OFFLINE && tx_ready;
    wire socket_active = state_q == S_ACTIVE && !logout_complete;
    wire caught_up = checkpoint == socket_cursor;
    wire socket_has_gap = socket_cursor > checkpoint;
    wire login_is_valid = valid_credentials({32'b0, s_login.username}, 6) &&
                          valid_credentials(s_login.password, 10) &&
                          valid_session(s_login.session, !has_checkpoint) &&
                          valid_timeout(s_login.heartbeat_ms) && s_login.sequence_start != 0 &&
                          ((!has_checkpoint && s_login.sequence_start == 1) ||
                           (has_checkpoint && s_login.session == session_q &&
                            s_login.sequence_start <= checkpoint));
    wire ack_id_is_valid = valid_session(inbound.session, 1'b0) &&
                           (login_q.session == BLANK_SESSION || inbound.session == login_q.session);
    wire timeout_due = RX_TIMEOUT_TICKS != 0 && state_q == S_ACTIVE &&
                       !logout_pending && !logout_complete &&
                       rx_idle >= 32'(RX_TIMEOUT_TICKS) && !s_rx_valid && !packet_valid;
    wire heartbeat_due = TX_HEARTBEAT_TICKS != 0 && state_q == S_ACTIVE &&
                         !logout_pending && !logout_complete &&
                         tx_idle >= 32'(TX_HEARTBEAT_TICKS);

    logic decimal_request, decimal_decode, decimal_output_ready;
    wire decimal_accept_ready, decimal_output_valid, decoded_sequence_ok;
    wire [63:0] decimal_number;
    wire [159:0] decimal_ascii;

    logic take_login, take_login_ack, finish_login, finish_logout, launch_logout;
    logic saw_heartbeat, send_heartbeat;
    logic drop_replay, next_event, set_market, market_value;
    logic this_fault;
    wire link_faultable = transport_up && !rst && state_q != S_FAULT && state_q != S_ENDED;

    assign m_notice_valid = !rst && notice_valid_q;
    assign m_notice = notice_q;
    assign metrics = metrics_q;
    assign session_state = state_q;
    assign session_id = session_q;
    assign next_sequence = checkpoint;
    assign last_fault = fault_q;
    assign replaying = socket_active && !caught_up;
    assign market_open = market_q;
    assign kill_latched = kill_q || kill_req;
    assign close_transport = transport_up &&
                             (state_q == S_FAULT || state_q == S_ENDED || logout_complete);
    assign finish_logout = logout_pending && tx_fire && m_tx_last;

    ouch_soup_rx rx (
        .clk, .flush(transport_flush), .s_valid(s_rx_valid), .s_ready(s_rx_ready),
        .s_data(s_rx_data), .s_keep(s_rx_keep), .m_valid(packet_valid), .m_ready(packet_ready),
        .m_packet(packet), .fault(parser_fault)
    );

    ouch_rx_decode decode (.frame(packet), .decoded(inbound));

    ouch_soup_tx tx (
        .clk, .flush(transport_flush), .s_valid(tx_submit), .s_ready(tx_ready),
        .s_packet(tx_packet), .m_valid(m_tx_valid), .m_ready(m_tx_ready),
        .m_data(m_tx_data), .m_keep(m_tx_keep), .m_last(m_tx_last)
    );

    ouch_decimal64 decimal (
        .clk, .flush(transport_flush), .s_valid(decimal_request), .s_ready(decimal_accept_ready),
        .s_decode(decimal_decode), .s_number(s_login.sequence_start), .s_text(inbound.ascii_sequence),
        .m_valid(decimal_output_valid), .m_ready(decimal_output_ready), .m_ok(decoded_sequence_ok),
        .m_number(decimal_number), .m_text(decimal_ascii)
    );

    ouch_order_table #(.ORDER_SLOTS(ORDER_SLOTS)) orders (
        .clk, .rst, .command(s_cmd), .cmd_session_status(local_status), .admission_status,
        .cmd_commit, .cmd_notice(local_notice), .cmd_packet(command_packet), .cmd_sends,
        .inbound, .sequence_id(socket_cursor), .rx_commit(order_commit), .rx_notice(order_notice),
        .query_slot, .query_view, .highest_order_ref, .live_count, .pending_count
    );

    always @* begin
        urgent_fault = ST_OK;
        if (parser_fault != ST_OK) urgent_fault = parser_fault;
        else if (timeout_due) urgent_fault = ST_RX_TIMEOUT;

        admission_status = ST_OK;
        if (kill_latched) admission_status = ST_KILLED;
        else if (!market_q) admission_status = ST_MARKET_CLOSED;

        local_status = ST_OK;
        if (s_cmd.kind != CMD_QUERY) begin
            if (!socket_active || !transport_up || !caught_up || logout_pending)
                local_status = ST_NOT_ACTIVE;
            else if (s_cmd.kind == CMD_ENTER || s_cmd.kind == CMD_REPLACE)
                local_status = admission_status;
        end
    end

    function automatic notice_t session_notice(
        input notice_kind_t kind, input status_t code,
        input logic [63:0] seq_id, input logic [15:0] reason
    );
        notice_t value;
        begin
            value = '0;
            value.kind = kind;
            value.status = code;
            value.slot = NO_SLOT;
            value.sequence_id = seq_id;
            value.reason = reason;
            session_notice = value;
        end
    endfunction

    always @* begin
        state_d = state_q;
        notice_d = '0;
        notice_write = 1'b0;
        s_login_ready = 1'b0;
        s_logout_ready = 1'b0;
        s_cmd_ready = 1'b0;
        tx_submit = 1'b0;
        tx_packet = '0;
        packet_ready = 1'b0;
        decimal_request = 1'b0;
        decimal_decode = 1'b0;
        decimal_output_ready = 1'b0;
        take_login = 1'b0;
        take_login_ack = 1'b0;
        finish_login = 1'b0;
        launch_logout = 1'b0;
        saw_heartbeat = 1'b0;
        send_heartbeat = 1'b0;
        cmd_commit = 1'b0;
        order_commit = 1'b0;
        drop_replay = 1'b0;
        next_event = 1'b0;
        set_market = 1'b0;
        market_value = 1'b0;
        this_fault = 1'b0;

        if (!rst) begin
            if (link_faultable && urgent_fault != ST_OK) begin
                if (space) begin
                    notice_d = session_notice(N_FAULT, urgent_fault, checkpoint, 0);
                    notice_write = 1'b1;
                    this_fault = 1'b1;
                end
            end else if (transport_up && packet_valid &&
                         (state_q == S_LOGIN_WAIT || socket_active)) begin
                if (space) begin
                    packet_ready = 1'b1;
                    if (inbound.status != ST_OK) begin
                        notice_d = session_notice(N_FAULT, inbound.status, socket_cursor, 0);
                        notice_write = 1'b1;
                        this_fault = 1'b1;
                    end else if (inbound.soup_type == "+") begin
                        // Debug packets carry no session or order state.
                    end else if (state_q == S_LOGIN_WAIT) begin
                        case (inbound.soup_type)
                            "A": if (ack_id_is_valid) begin
                                packet_ready = decimal_accept_ready;
                                take_login_ack = decimal_accept_ready;
                                decimal_request = decimal_accept_ready;
                                decimal_decode = 1'b1;
                                if (decimal_accept_ready) state_d = S_LOGIN_PARSE;
                            end else begin
                                notice_d = session_notice(N_FAULT, ST_SESSION_ID, checkpoint, 0);
                                notice_write = 1'b1;
                                this_fault = 1'b1;
                            end
                            "J": begin
                                notice_d = session_notice(N_LOGIN_REJECT, ST_LOGIN_REJECTED,
                                                          checkpoint, inbound.reason);
                                notice_write = 1'b1;
                                this_fault = 1'b1;
                            end
                            default: begin
                                notice_d = session_notice(N_FAULT, ST_LOGIN_PHASE, checkpoint, 0);
                                notice_write = 1'b1;
                                this_fault = 1'b1;
                            end
                        endcase
                    end else begin
                        case (inbound.soup_type)
                            "H": saw_heartbeat = 1'b1;
                            "Z": begin
                                state_d = S_ENDED;
                                notice_d = session_notice(N_END, ST_OK, checkpoint, 0);
                                notice_write = 1'b1;
                            end
                            "S": begin
                                notice_write = 1'b1;
                                if (socket_has_gap) begin
                                    notice_d = session_notice(N_FAULT, ST_SEQUENCE_GAP, socket_cursor, 0);
                                    this_fault = 1'b1;
                                end else if (&socket_cursor) begin
                                    notice_d = session_notice(N_FAULT, ST_SEQUENCE_OVERFLOW, socket_cursor, 0);
                                    this_fault = 1'b1;
                                end else if (!caught_up) begin
                                    notice_d = session_notice(N_REPLAY, ST_OK, socket_cursor, 0);
                                    drop_replay = 1'b1;
                                end else if (inbound.ouch_type == "S") begin
                                    notice_d = session_notice(N_SYSTEM, ST_OK, socket_cursor, inbound.reason);
                                    notice_d.timestamp = inbound.timestamp;
                                    if (inbound.reason == 16'h0053 || inbound.reason == 16'h0045) begin
                                        next_event = 1'b1;
                                        set_market = 1'b1;
                                        market_value = inbound.reason == 16'h0053;
                                    end else begin
                                        notice_d.kind = N_FAULT;
                                        notice_d.status = ST_BAD_FIELDS;
                                        this_fault = 1'b1;
                                    end
                                end else begin
                                    notice_d = order_notice;
                                    if (order_notice.status == ST_OK) begin
                                        order_commit = 1'b1;
                                        next_event = 1'b1;
                                    end else begin
                                        notice_d.kind = N_FAULT;
                                        this_fault = 1'b1;
                                    end
                                end
                            end
                            default: begin
                                notice_d = session_notice(N_FAULT, ST_LOGIN_PHASE, checkpoint, 0);
                                notice_write = 1'b1;
                                this_fault = 1'b1;
                            end
                        endcase
                    end
                end
            end else if (transport_up && state_q == S_LOGIN_BUILD && decimal_output_valid) begin
                if (tx_ready) begin
                    tx_packet = login_packet(login_q.username, login_q.password, login_q.session,
                                             decimal_ascii, login_q.heartbeat_ms);
                    tx_submit = 1'b1;
                    decimal_output_ready = 1'b1;
                    state_d = S_LOGIN_SEND;
                end
            end else if (transport_up && state_q == S_LOGIN_PARSE && decimal_output_valid) begin
                if (space) begin
                    decimal_output_ready = 1'b1;
                    notice_write = 1'b1;
                    if (!decoded_sequence_ok || decimal_number == 0) begin
                        notice_d = session_notice(N_FAULT, ST_LOGIN_TEXT, checkpoint, 0);
                        this_fault = 1'b1;
                    end else if ((has_checkpoint && decimal_number > checkpoint) ||
                                 (!has_checkpoint && decimal_number != login_q.sequence_start)) begin
                        notice_d = session_notice(N_FAULT, ST_SEQUENCE_GAP, decimal_number, 0);
                        this_fault = 1'b1;
                    end else begin
                        notice_d = session_notice(N_LOGIN, ST_OK, decimal_number, 0);
                        state_d = S_ACTIVE;
                        finish_login = 1'b1;
                    end
                end
            end else if (transport_up && login_port_idle && s_login_valid) begin
                s_login_ready = space && decimal_accept_ready;
                if (s_login_ready) begin
                    if (login_is_valid) begin
                        take_login = 1'b1;
                        decimal_request = 1'b1;
                        state_d = S_LOGIN_BUILD;
                    end else begin
                        notice_d = session_notice(N_LOGIN_REJECT, ST_LOGIN_TEXT, checkpoint, 0);
                        notice_write = 1'b1;
                    end
                end
            end else if (transport_up && socket_active && !logout_pending && s_logout_valid) begin
                s_logout_ready = tx_ready;
                if (s_logout_ready) begin
                    tx_submit = 1'b1;
                    tx_packet = control_packet("O");
                    launch_logout = 1'b1;
                end
            end else if (s_cmd_valid) begin
                s_cmd_ready = space && (!cmd_sends || tx_ready);
                if (s_cmd_ready) begin
                    cmd_commit = 1'b1;
                    notice_d = local_notice;
                    notice_write = 1'b1;
                    if (cmd_sends) begin
                        tx_packet = command_packet;
                        tx_submit = 1'b1;
                    end
                end
            end else if (transport_up && login_port_idle) begin
                s_login_ready = space && decimal_accept_ready;
            end

            // A local notice or stalled RX notice does not transmit any bytes.
            // Reuse the idle serializer unless another frame was selected.
            if (transport_up && heartbeat_due && tx_ready && !tx_submit &&
                urgent_fault == ST_OK && !this_fault && state_d == S_ACTIVE) begin
                tx_submit = 1'b1;
                tx_packet = control_packet("R");
                send_heartbeat = 1'b1;
            end
            if (transport_up && state_q == S_LOGIN_SEND && tx_fire && m_tx_last)
                state_d = S_LOGIN_WAIT;
            if (this_fault)
                state_d = S_FAULT;
            if (!transport_up && state_q != S_ENDED)
                state_d = S_OFFLINE;
        end
    end

    always @(posedge clk) begin
        if (rst) begin
            state_q <= S_OFFLINE;
            fault_q <= ST_OK;
            has_checkpoint <= 1'b0;
            checkpoint <= 1;
            socket_cursor <= 1;
            session_q <= BLANK_SESSION;
            accepted_session <= BLANK_SESSION;
            market_q <= 1'b0;
            kill_q <= 1'b0;
            notice_valid_q <= 1'b0;
            notice_q <= '0;
            login_q <= '0;
            logout_pending <= 1'b0;
            logout_complete <= 1'b0;
            metrics_q <= '0;
        end else begin
            state_q <= state_d;
            kill_q <= kill_latched;
            if (space)
                notice_valid_q <= notice_write;
            if (notice_write)
                notice_q <= notice_d;

            if (take_login)
                login_q <= s_login;
            else if ((state_q == S_LOGIN_BUILD && tx_submit) || !transport_up)
                login_q.password <= '0;
            if (take_login_ack)
                accepted_session <= inbound.session;
            if (finish_login) begin
                has_checkpoint <= 1'b1;
                session_q <= accepted_session;
                socket_cursor <= decimal_number;
                if (!has_checkpoint) checkpoint <= decimal_number;
                fault_q <= ST_OK;
            end else if (next_event) begin
                checkpoint <= socket_cursor + 1'b1;
                socket_cursor <= socket_cursor + 1'b1;
            end else if (drop_replay) begin
                socket_cursor <= socket_cursor + 1'b1;
            end

            if (set_market)
                market_q <= market_value;
            if (state_d == S_ENDED)
                market_q <= 1'b0;
            if (!transport_up) begin
                logout_pending <= 1'b0;
                logout_complete <= 1'b0;
            end else if (finish_logout) begin
                logout_pending <= 1'b0;
                logout_complete <= 1'b1;
            end else if (launch_logout) begin
                logout_pending <= 1'b1;
            end

            if (this_fault) begin
                fault_q <= notice_d.status;
                metrics_q.fault_count <= incr_counter(metrics_q.fault_count);
            end
            if (packet_valid && packet_ready)
                metrics_q.socket_frames <= incr_counter(metrics_q.socket_frames);
            if (next_event)
                metrics_q.decoded_events <= incr_counter(metrics_q.decoded_events);
            if (drop_replay)
                metrics_q.replayed <= incr_counter(metrics_q.replayed);
            if (saw_heartbeat)
                metrics_q.peer_heartbeats <= incr_counter(metrics_q.peer_heartbeats);
            if (send_heartbeat)
                metrics_q.client_heartbeats <= incr_counter(metrics_q.client_heartbeats);
            if (cmd_commit) begin
                if (local_notice.status != ST_OK)
                    metrics_q.local_rejects <= incr_counter(metrics_q.local_rejects);
                else if (s_cmd.kind == CMD_RETRY)
                    metrics_q.retries <= incr_counter(metrics_q.retries);
                else if (cmd_sends)
                    metrics_q.sent_commands <= incr_counter(metrics_q.sent_commands);
            end
        end
    end

    always @(posedge clk) begin
        if (transport_flush || state_q != S_ACTIVE) begin
            rx_idle <= 0;
            tx_idle <= 0;
        end else begin
            if (s_rx_valid || packet_valid)
                rx_idle <= 0;
            else if (time_tick && s_rx_ready)
                rx_idle <= incr_counter(rx_idle);

            if (tx_fire || tx_submit)
                tx_idle <= 0;
            else if (time_tick && tx_ready)
                tx_idle <= incr_counter(tx_idle);
        end
    end
endmodule

`default_nettype wire
