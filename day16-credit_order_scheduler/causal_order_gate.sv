`timescale 1ns/1ps
`default_nettype none

// Gate an order on evidence that its earlier same-session dependency
// completed. Independent rows can dispatch, settle, or expire out of order.
module causal_order_gate #(
    parameter integer SESSIONS = 3,
    parameter integer WINDOW = 4,
    parameter integer EPOCH_W = 16,
    parameter integer SEQ_W = 32,
    parameter integer TTL_W = 16,
    parameter integer CREDIT_W = 8,
    parameter integer TAG_W = 16,
    parameter integer PAYLOAD_W = 32,
    localparam integer SESSION_W = $clog2(SESSIONS+1)
) (
    input wire clk,
    input wire rst,
    input wire c_valid,
    output wire c_ready,
    input wire [1:0] c_op,
    input wire [SESSION_W-1:0] c_session,
    input wire [EPOCH_W-1:0] c_expected_epoch,
    input wire [SEQ_W-1:0] c_sequence,
    output wire reply_valid,
    input wire reply_ready,
    output logic [3:0] reply_status,
    output logic [SESSION_W-1:0] reply_session,
    output logic [EPOCH_W-1:0] reply_epoch,
    output logic [SEQ_W-1:0] reply_next_sequence,
    output logic [1:0] reply_row_state,
    output logic reply_armed,
    output logic reply_exhausted,

    input wire s_valid,
    output wire s_ready,
    input wire [SESSION_W-1:0] s_session,
    input wire [EPOCH_W-1:0] s_epoch,
    input wire [SEQ_W-1:0] s_sequence,
    input wire s_dependent,
    input wire [SEQ_W-1:0] s_predecessor,
    input wire [TTL_W-1:0] s_lifetime,
    input wire [CREDIT_W-1:0] s_cost,
    input wire [TAG_W-1:0] s_tag,
    input wire [PAYLOAD_W-1:0] s_payload,
    output logic [3:0] s_status,

    output wire m_valid,
    input wire m_ready,
    output wire m_applicable,
    output wire [SESSION_W-1:0] m_session,
    output wire [EPOCH_W-1:0] m_epoch,
    output wire [SEQ_W-1:0] m_sequence,
    output wire [CREDIT_W-1:0] m_cost,
    output wire [TAG_W-1:0] m_tag,
    output wire [PAYLOAD_W-1:0] m_payload,

    input wire ack_valid,
    output wire ack_ready,
    input wire [SESSION_W-1:0] ack_session,
    input wire [EPOCH_W-1:0] ack_epoch,
    input wire [SEQ_W-1:0] ack_sequence,
    input wire ack_success,

    output wire d_valid,
    input wire d_ready,
    output wire [SESSION_W-1:0] d_session,
    output wire [EPOCH_W-1:0] d_epoch,
    output wire [SEQ_W-1:0] d_sequence,
    output wire [TAG_W-1:0] d_tag,
    output wire [2:0] d_status,
    output logic [31:0] stale_acks,
    output logic [31:0] duplicate_acks,
    output logic [31:0] conflicting_acks
);
    localparam integer ROWS=SESSIONS*WINDOW;
    localparam integer ROW_W=ROWS>1 ? $clog2(ROWS) : 1;
    localparam integer BANK_W=SESSIONS>1 ? $clog2(SESSIONS) : 1;
    localparam logic [1:0] OPEN=0, FENCE=1, RECLAIM=2, INSPECT=3;
    localparam logic [1:0] EMPTY=0, WAITING=1, SENT=2, DONE=3;
    localparam logic [2:0] SUCCESS=0, VENUE_FAILURE=1, DEPENDENCY_FAILED=2, EXPIRED=3, FENCED=4;
    localparam logic [3:0] OK=0, BAD_SESSION=1, STALE_EPOCH=2, NOT_OPEN=3,
                           BAD_SEQUENCE=4, BAD_SHAPE=5, BAD_DEPENDENCY=6, EXHAUSTED=7;
    localparam logic [3:0] BUSY=3, EPOCH_FULL=4, MISSING=5;

    logic [SESSIONS-1:0] armed, exhausted;
    logic [EPOCH_W-1:0] epoch [0:SESSIONS-1];
    logic [SEQ_W-1:0] next_sequence [0:SESSIONS-1];
    logic [1:0] state [0:ROWS-1];
    logic [SEQ_W-1:0] row_sequence [0:ROWS-1];
    logic [TAG_W-1:0] row_tag [0:ROWS-1];
    logic [PAYLOAD_W-1:0] payloads [0:ROWS-1];
    logic [CREDIT_W-1:0] costs [0:ROWS-1];
    logic [TTL_W-1:0] ttl [0:ROWS-1];
    logic [ROW_W-1:0] predecessor [0:ROWS-1];
    logic [ROWS-1:0] dependent, submitted, unread;
    logic [2:0] terminal [0:ROWS-1];
    wire [ROWS-1:0] ready_to_send, local_failure;
    wire [2:0] local_code [0:ROWS-1];

    wire s_key=int'(s_session)<SESSIONS;
    wire c_key=int'(c_session)<SESSIONS;
    wire ack_key=int'(ack_session)<SESSIONS;
    wire [BANK_W-1:0] s_bank=s_key ? BANK_W'(s_session) : '0;
    wire [BANK_W-1:0] c_bank=c_key ? BANK_W'(c_session) : '0;
    wire [BANK_W-1:0] ack_bank=ack_key ? BANK_W'(ack_session) : '0;
    logic has_space,has_predecessor,control_found,ack_found,session_empty,parent_pinned;
    logic [ROW_W-1:0] free_slot, parent_slot, control_slot, ack_slot;
    logic receipt_pending;
    logic [3:0] control_status;
    logic [ROW_W-1:0] send_cursor, send_slot, send_hold, result_cursor, result_slot, result_hold;
    logic send_found,result_found,held,published;
    wire [ROW_W-1:0] result_index=published ? result_hold : result_slot;
    wire [BANK_W-1:0] send_bank=BANK_W'(int'(send_hold)/WINDOW);
    wire [BANK_W-1:0] result_bank=BANK_W'(int'(result_index)/WINDOW);

    assign reply_valid=!rst && receipt_pending;
    assign c_ready=!rst && (!receipt_pending || reply_ready);
    wire take_control=c_valid && c_ready;
    wire opens=take_control && control_status==OK && c_op==OPEN;
    wire closes=take_control && control_status==OK && c_op==FENCE;
    wire reclaims=take_control && control_status==OK && c_op==RECLAIM;
    wire changes_bank=(opens || closes) && c_bank==s_bank;
    assign ack_ready=!rst;
    wire take_ack=ack_valid && ack_ready;
    wire matching_ack=ack_found && ack_epoch==epoch[ack_bank] && ack_key;
    wire duplicate=matching_ack && state[ack_slot]==DONE && submitted[ack_slot];
    wire conflict=take_ack && duplicate && ack_success!=(terminal[ack_slot]==SUCCESS);

    assign s_ready=!rst && (s_status!=OK || has_space);
    wire enqueues=s_valid && s_ready && s_status==OK;
    assign m_valid=!rst && held;
    assign m_applicable=m_valid && ready_to_send[send_hold];
    assign {m_session,m_epoch,m_sequence,m_cost,m_tag,m_payload}=
        {SESSION_W'(send_bank),epoch[send_bank],row_sequence[send_hold],costs[send_hold],
         row_tag[send_hold],payloads[send_hold]};
    assign d_valid=!rst && (published || result_found);
    assign {d_session,d_epoch,d_sequence,d_tag,d_status}=
        {SESSION_W'(result_bank),epoch[result_bank],row_sequence[result_index],
         row_tag[result_index],terminal[result_index]};

    function automatic logic [ROW_W-1:0] next_row(input logic [ROW_W-1:0] address);
        next_row=address==ROW_W'(ROWS-1) ? '0 : address+1'b1;
    endfunction

    function automatic logic [31:0] increment(input logic [31:0] count);
        increment=(&count) ? count : count+1'b1;
    endfunction

    always @* begin : lookup
        has_space=0;
        has_predecessor=0;
        control_found=0;
        ack_found=0;
        session_empty=1;
        free_slot='0;
        parent_slot='0;
        control_slot='0;
        ack_slot='0;
        for(integer row=0;row<ROWS;row=row+1) begin
            if (row/WINDOW==int'(s_bank)) begin
                if (state[row]==EMPTY && !has_space) begin
                    has_space=1;
                    free_slot=ROW_W'(row);
                end
                if (state[row]!=EMPTY && row_sequence[row]==s_predecessor) begin
                    has_predecessor=1;
                    parent_slot=ROW_W'(row);
                end
            end
            if (row/WINDOW==int'(c_bank) && state[row]!=EMPTY) begin
                session_empty=0;
                if (row_sequence[row]==c_sequence) begin
                    control_found=1;
                    control_slot=ROW_W'(row);
                end
            end
            if (row/WINDOW==int'(ack_bank) && state[row]!=EMPTY && row_sequence[row]==ack_sequence) begin
                ack_found=1;
                ack_slot=ROW_W'(row);
            end
        end
        parent_pinned=0;
        for(integer row=0;row<ROWS;row=row+1)
            if (state[row]==WAITING && dependent[row] && predecessor[row]==control_slot)
                parent_pinned=1;
    end

    always @* begin
        control_status=OK;
        if (!c_key)
            control_status=BAD_SESSION;
        else if (c_op!=INSPECT && c_expected_epoch!=epoch[c_bank])
            control_status=STALE_EPOCH;
        else if (c_op==OPEN) begin
            if (&epoch[c_bank])
                control_status=EPOCH_FULL;
            else if (!session_empty)
                control_status=BUSY;
        end else if (c_op==RECLAIM) begin
            if (!control_found)
                control_status=MISSING;
            else if (state[control_slot]!=DONE || unread[control_slot] || parent_pinned ||
                     (held && send_hold==control_slot))
                control_status=BUSY;
        end

        s_status=OK;
        if (!s_key)
            s_status=BAD_SESSION;
        else if (s_epoch!=epoch[s_bank])
            s_status=STALE_EPOCH;
        else if (!armed[s_bank] || changes_bank || (conflict && s_bank==ack_bank))
            s_status=NOT_OPEN;
        else if (exhausted[s_bank])
            s_status=EXHAUSTED;
        else if (s_sequence!=next_sequence[s_bank])
            s_status=BAD_SEQUENCE;
        else if (s_lifetime==0 || s_cost==0)
            s_status=BAD_SHAPE;
        else if (s_dependent && (!has_predecessor || s_predecessor>=s_sequence ||
                                 (reclaims && control_slot==parent_slot)))
            s_status=BAD_DEPENDENCY;
    end

    for(genvar row=0;row<ROWS;row=row+1) begin : eligibility
        localparam logic [BANK_W-1:0] BANK=BANK_W'(row/WINDOW);
        wire active_bank=armed[BANK] && !(closes && c_bank==BANK) &&
                         !(conflict && ack_bank==BANK);
        wire predecessor_done=state[predecessor[row]]==DONE;
        wire predecessor_ok=predecessor_done && terminal[predecessor[row]]==SUCCESS;
        wire predecessor_failed=dependent[row] && predecessor_done && !predecessor_ok;
        assign ready_to_send[row]=state[row]==WAITING && active_bank && ttl[row]!=0 &&
                                  (!dependent[row] || predecessor_ok);
        assign local_failure[row]=state[row]==WAITING &&
                                  (!active_bank || ttl[row]==0 || predecessor_failed);
        assign local_code[row]=!active_bank ? FENCED :
                              ttl[row]==0 ? EXPIRED : DEPENDENCY_FAILED;
    end

    always @* begin : arbitration
        integer data_at, done_at;
        send_found=0;
        result_found=0;
        send_slot='0;
        result_slot='0;
        for(integer offset=0;offset<ROWS;offset=offset+1) begin
            data_at=int'(send_cursor)+offset;
            done_at=int'(result_cursor)+offset;
            if (data_at>=ROWS) data_at=data_at-ROWS;
            if (done_at>=ROWS) done_at=done_at-ROWS;
            if (!send_found && ready_to_send[data_at]) begin
                send_found=1;
                send_slot=ROW_W'(data_at);
            end
            if (!result_found && unread[done_at]) begin
                result_found=1;
                result_slot=ROW_W'(done_at);
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            {armed,exhausted,dependent,submitted,unread} <= '0;
            {receipt_pending,reply_status,reply_session,reply_epoch,reply_next_sequence,
             reply_row_state,reply_armed,reply_exhausted} <= '0;
            {send_cursor,send_hold,result_cursor,result_hold,held,published} <= '0;
            {stale_acks,duplicate_acks,conflicting_acks} <= '0;
            for(integer bank=0;bank<SESSIONS;bank=bank+1)
                {epoch[bank],next_sequence[bank]} <= '0;
            for(integer row=0;row<ROWS;row=row+1)
                {state[row],row_sequence[row],row_tag[row],payloads[row],
                 costs[row],ttl[row],predecessor[row],terminal[row]} <= '0;
        end else begin
            if (c_ready) begin
                receipt_pending <= c_valid;
                if (take_control) begin
                    {reply_status,reply_session} <= {control_status,c_session};
                    reply_epoch <= c_key ? epoch[c_bank]+EPOCH_W'(opens) : '0;
                    reply_next_sequence <= !c_key ? '0 : opens ? c_sequence : next_sequence[c_bank];
                    reply_row_state <= control_found && !reclaims && c_key ? state[control_slot] : EMPTY;
                    reply_armed <= c_key && (opens || (!closes && armed[c_bank]));
                    reply_exhausted <= c_key && !opens && exhausted[c_bank];
                end
            end
            if (opens) begin
                armed[c_bank]<=1;
                exhausted[c_bank]<=0;
                epoch[c_bank]<=epoch[c_bank]+1'b1;
                next_sequence[c_bank]<=c_sequence;
            end else if (closes) begin
                armed[c_bank]<=0;
            end
            if (enqueues) begin
                state[free_slot]<=WAITING;
                {row_sequence[free_slot],row_tag[free_slot],payloads[free_slot],costs[free_slot],ttl[free_slot]}<=
                    {s_sequence,s_tag,s_payload,s_cost,s_lifetime};
                dependent[free_slot]<=s_dependent;
                predecessor[free_slot]<=parent_slot;
                submitted[free_slot]<=0;
                unread[free_slot]<=0;
                if (&s_sequence)
                    exhausted[s_bank]<=1;
                else
                    next_sequence[s_bank]<=s_sequence+1'b1;
            end
            if (reclaims)
                state[control_slot]<=EMPTY;

            for(integer row=0;row<ROWS;row=row+1) begin
                if (state[row]==WAITING && ttl[row]!=0)
                    ttl[row]<=ttl[row]-1'b1;
                if (local_failure[row]) begin
                    state[row]<=DONE;
                    terminal[row]<=local_code[row];
                    unread[row]<=1;
                end
            end

            if (m_valid && m_ready) begin
                held<=0;
                if (m_applicable) begin
                    state[send_hold]<=SENT;
                    submitted[send_hold]<=1;
                end
            end else if (!held && send_found) begin
                held<=1;
                send_hold<=send_slot;
                send_cursor<=next_row(send_slot);
            end

            if (take_ack) begin
                if (matching_ack && state[ack_slot]==SENT) begin
                    state[ack_slot]<=DONE;
                    terminal[ack_slot]<=ack_success ? SUCCESS : VENUE_FAILURE;
                    unread[ack_slot]<=1;
                end else if (conflict) begin
                    conflicting_acks<=increment(conflicting_acks);
                    armed[ack_bank]<=0;
                end else if (duplicate) begin
                    duplicate_acks<=increment(duplicate_acks);
                end else begin
                    stale_acks<=increment(stale_acks);
                end
            end

            if (d_valid && d_ready) begin
                published<=0;
                unread[result_index]<=0;
                result_cursor<=next_row(result_index);
            end else if (d_valid) begin
                published<=1;
                result_hold<=result_index;
            end
        end
    end
`ifdef FORMAL_CAUSAL
    logic sampled=0;
    always_ff @(posedge clk) begin
        sampled<=1;
        if (!sampled)
            assume(rst);
        if (sampled && !rst && !$past(rst)) begin
            if ($past(m_valid && !m_ready))
                assert(m_valid && $stable({m_session,m_epoch,m_sequence,m_cost,m_tag,m_payload}));
            if ($past(d_valid && !d_ready))
                assert(d_valid && $stable({d_session,d_epoch,d_sequence,d_tag,d_status}));
            if (m_applicable && dependent[send_hold])
                assert (state[predecessor[send_hold]]==DONE && terminal[predecessor[send_hold]]==SUCCESS);
            if (m_applicable)
                assert (armed[send_bank] && ttl[send_hold] != 0 && state[send_hold]==WAITING);
            if (m_valid)
                assert (state[send_hold]!=EMPTY);
            for (integer row=0;row<ROWS;row=row+1) begin
                if (state[row]==WAITING && dependent[row]) begin
                    assert (state[predecessor[row]]!=EMPTY);
                    assert (row_sequence[predecessor[row]]<row_sequence[row]);
                    assert (row/WINDOW==int'(predecessor[row])/WINDOW);
                end
                if (unread[row])
                    assert (state[row]==DONE);
            end
            cover (m_applicable && m_ready && dependent[send_hold]);
            cover (m_valid && m_ready && !m_applicable);
            cover (d_valid && d_status==DEPENDENCY_FAILED);
            cover (conflict);
        end
    end
`endif

endmodule

`default_nettype wire
