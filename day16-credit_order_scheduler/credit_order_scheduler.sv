`timescale 1ns/1ps
`default_nettype none

// Queue orders by destination and meter each destination with explicit
// credits returned by the downstream adapter. Order eligibility never
// depends on the sink's ready signal.
module credit_order_scheduler #(
    parameter integer CLASSES = 3,
    parameter integer DEPTH = 4,
    parameter integer CREDIT_W = 8,
    parameter integer TAG_W = 16,
    parameter integer PAYLOAD_W = 32,
    parameter integer CLASS_W = (CLASSES > 1) ? $clog2(CLASSES) : 1,
    parameter integer COUNT_W = $clog2(DEPTH + 1)
) (
    input  wire                 clk,
    input  wire                 rst,
    input  wire                 s_valid,
    output wire                 s_ready,
    input  wire [CLASS_W-1:0]   s_class,
    input  wire [CREDIT_W-1:0]  s_cost,
    input  wire [TAG_W-1:0]     s_tag,
    input  wire [PAYLOAD_W-1:0] s_payload,
    output wire                 s_rejected,

    output wire                 m_valid,
    input  wire                 m_ready,
    output wire [CLASS_W-1:0]   m_class,
    output wire [CREDIT_W-1:0]  m_cost,
    output wire [TAG_W-1:0]     m_tag,
    output wire [PAYLOAD_W-1:0] m_payload,

    input  wire                 ctl_valid,
    output wire                 ctl_ready,
    input  wire [1:0]           ctl_op,
    input  wire [CLASS_W-1:0]   ctl_class,
    input  wire [CREDIT_W-1:0]  ctl_amount,
    input  wire [3:0]           ctl_weight,

    output wire                 report_valid,
    input  wire                 report_ready,
    output logic [1:0]          report_op,
    output logic [CLASS_W-1:0]  report_class,
    output logic [2:0]          report_status,
    output logic [CREDIT_W-1:0] report_capacity,
    output logic [CREDIT_W-1:0] report_available,
    output logic [CREDIT_W-1:0] report_inflight,
    output logic [COUNT_W-1:0]  report_queued,
    output logic [3:0]          report_weight
);
    localparam integer ADDR_W = (DEPTH > 1) ? $clog2(DEPTH) : 1;
    localparam logic [1:0] CONFIGURE = 0, REFUND = 1, INSPECT = 2;
    localparam logic [2:0] OK = 0, BAD_CLASS = 1, BAD_FIELDS = 2,
                          BUSY = 3, OVER_REFUND = 4;

    logic [ADDR_W-1:0] read_ptr [0:CLASSES-1];
    logic [ADDR_W-1:0] write_ptr [0:CLASSES-1];
    logic [COUNT_W-1:0] queued [0:CLASSES-1];
    logic [TAG_W-1:0] tags [0:CLASSES-1][0:DEPTH-1];
    logic [PAYLOAD_W-1:0] payloads [0:CLASSES-1][0:DEPTH-1];
    logic [CREDIT_W-1:0] costs [0:CLASSES-1][0:DEPTH-1];
    logic [CREDIT_W-1:0] capacity [0:CLASSES-1];
    logic [CREDIT_W-1:0] available [0:CLASSES-1];
    logic [CREDIT_W-1:0] inflight [0:CLASSES-1];
    logic [3:0] weight [0:CLASSES-1];

    logic [CLASS_W-1:0] cursor;
    logic [3:0] served_here;
    logic held;
    logic [CLASS_W-1:0] held_class;
    logic candidate_valid;
    logic [CLASS_W-1:0] candidate;
    logic pending_report;

    // Wrap even for an odd number of configured classes.
    function automatic logic [CLASS_W-1:0] next_class(input logic [CLASS_W-1:0] current);
        next_class = (current == CLASS_W'(CLASSES - 1)) ? '0 : current + 1'b1;
    endfunction

    // A read or write cursor wraps at actual FIFO depth rather than bit width.
    function automatic logic [ADDR_W-1:0] next_slot(input logic [ADDR_W-1:0] current);
        next_slot = (current == ADDR_W'(DEPTH - 1)) ? '0 : current + 1'b1;
    endfunction

    always_comb begin : choose_class
        integer index;
        candidate_valid = 1'b0;
        candidate = '0;
        for (integer offset = 0; offset < CLASSES; offset = offset + 1) begin
            index = int'(cursor) + offset;
            if (index >= CLASSES)
                index = index - CLASSES;
            if (!candidate_valid && queued[index] != 0 &&
                costs[index][read_ptr[index]] <= available[index]) begin
                candidate = CLASS_W'(index);
                candidate_valid = 1'b1;
            end
        end
    end

    wire [CLASS_W-1:0] selected = held ? held_class : candidate;
    wire [ADDR_W-1:0] head = read_ptr[selected];
    assign m_valid = !rst && (held || candidate_valid);
    assign m_class = m_valid ? selected : '0;
    assign m_cost = m_valid ? costs[selected][head] : '0;
    assign m_tag = m_valid ? tags[selected][head] : '0;
    assign m_payload = m_valid ? payloads[selected][head] : '0;

    wire pop = m_valid && m_ready;
    wire valid_input_class = int'(s_class) < CLASSES;
    wire [CLASS_W-1:0] input_index = valid_input_class ? s_class : '0;
    wire invalid_input = !valid_input_class || s_cost == 0;
    wire space = queued[input_index] < COUNT_W'(DEPTH) ||
                 (pop && selected == input_index);
    assign s_ready = !rst && (invalid_input || space);
    assign s_rejected = s_valid && s_ready && invalid_input;
    wire push = s_valid && s_ready && !invalid_input;

    wire valid_control_class = int'(ctl_class) < CLASSES;
    wire [CLASS_W-1:0] control_index = valid_control_class ? ctl_class : '0;
    assign report_valid = pending_report && !rst;
    assign ctl_ready = !rst && (!pending_report || report_ready);
    wire apply_control = ctl_valid && ctl_ready;
    logic [2:0] status;

    always_comb begin
        status = OK;
        if (!valid_control_class)
            status = BAD_CLASS;
        else begin
            case (ctl_op)
                CONFIGURE: if (ctl_weight == 0)
                    status = BAD_FIELDS;
                else if (inflight[control_index] != 0 || (m_valid && selected == control_index))
                    status = BUSY;
                REFUND: if (ctl_amount == 0 || ctl_weight != 0)
                    status = BAD_FIELDS;
                else if (ctl_amount > inflight[control_index])
                    status = OVER_REFUND;
                INSPECT: if (ctl_amount != 0 || ctl_weight != 0)
                    status = BAD_FIELDS;
                default: status = BAD_FIELDS;
            endcase
        end
    end

    wire configure = apply_control && status == OK && ctl_op == CONFIGURE;
    wire refund = apply_control && status == OK && ctl_op == REFUND;
    wire [CREDIT_W-1:0] cost_for_report =
        pop && selected == control_index ? m_cost : '0;
    wire [CREDIT_W-1:0] refund_for_report = refund ? ctl_amount : '0;
    wire [COUNT_W-1:0] next_report_count = queued[control_index]
        + COUNT_W'(push && input_index == control_index)
        - COUNT_W'(pop && selected == control_index);
    wire [4:0] visits = selected == cursor ? {1'b0, served_here} + 5'd1 : 5'd1;

    always_ff @(posedge clk) begin
        if (rst) begin
            cursor <= '0;
            served_here <= '0;
            held <= 1'b0;
            held_class <= '0;
            pending_report <= 1'b0;
            report_op <= '0;
            report_class <= '0;
            report_status <= '0;
            report_capacity <= '0;
            report_available <= '0;
            report_inflight <= '0;
            report_queued <= '0;
            report_weight <= '0;
        end else begin
            held <= m_valid && !m_ready;
            if (m_valid && !m_ready)
                held_class <= selected;
            if (pop) begin
                if (visits < {1'b0, weight[selected]}) begin
                    cursor <= selected;
                    served_here <= visits[3:0];
                end else begin
                    cursor <= next_class(selected);
                    served_here <= '0;
                end
            end else if (configure && control_index == cursor) begin
                served_here <= '0;
            end

            if (ctl_ready) begin
                pending_report <= ctl_valid;
                if (ctl_valid) begin
                    report_op <= ctl_op;
                    report_class <= ctl_class;
                    report_status <= status;
                    report_capacity <= !valid_control_class ? '0
                                     : (configure ? ctl_amount : capacity[control_index]);
                    report_available <= !valid_control_class ? '0
                                      : (configure ? ctl_amount :
                                         available[control_index] - cost_for_report + refund_for_report);
                    report_inflight <= !valid_control_class || configure ? '0
                                     : inflight[control_index] - refund_for_report + cost_for_report;
                    report_queued <= valid_control_class ? next_report_count : '0;
                    report_weight <= !valid_control_class ? '0
                                   : (configure ? ctl_weight : weight[control_index]);
                end
            end
        end
    end

    for (genvar key = 0; key < CLASSES; key = key + 1) begin : class_state
        wire local_push = push && input_index == CLASS_W'(key);
        wire local_pop = pop && selected == CLASS_W'(key);
        wire local_config = configure && control_index == CLASS_W'(key);
        wire local_refund = refund && control_index == CLASS_W'(key);
        wire [CREDIT_W-1:0] debit = local_pop ? m_cost : '0;
        wire [CREDIT_W-1:0] returned = local_refund ? ctl_amount : '0;

        always_ff @(posedge clk) begin
            if (rst) begin
                read_ptr[key] <= '0;
                write_ptr[key] <= '0;
                queued[key] <= '0;
                capacity[key] <= '0;
                available[key] <= '0;
                inflight[key] <= '0;
                weight[key] <= 4'd1;
            end else begin
                if (local_push) begin
                    tags[key][write_ptr[key]] <= s_tag;
                    payloads[key][write_ptr[key]] <= s_payload;
                    costs[key][write_ptr[key]] <= s_cost;
                    write_ptr[key] <= next_slot(write_ptr[key]);
                end
                if (local_pop)
                    read_ptr[key] <= next_slot(read_ptr[key]);
                if (local_push != local_pop)
                    queued[key] <= local_push ? queued[key] + 1'b1 : queued[key] - 1'b1;
                if (local_config) begin
                    capacity[key] <= ctl_amount;
                    available[key] <= ctl_amount;
                    inflight[key] <= '0;
                    weight[key] <= ctl_weight;
                end else if (local_pop || local_refund) begin
                    available[key] <= available[key] - debit + returned;
                    inflight[key] <= inflight[key] - returned + debit;
                end
            end
        end
    end
// The formal build retains the production state and checks its conservation
// laws directly. The reduced data widths still exercise every control path.
`ifdef FORMAL_SCHEDULER
    logic sampled = 0;
    always @(posedge clk) begin
        sampled <= 1'b1;
        if (!sampled)
            assume(rst);
        if (rst)
            assert(!s_ready && !m_valid && !ctl_ready && !report_valid);
        if (sampled && !rst && !$past(rst)) begin
            if ($past(m_valid && !m_ready)) begin
                assert(m_valid);
                assert({m_class, m_cost, m_tag, m_payload} ==
                       $past({m_class, m_cost, m_tag, m_payload}));
            end
            if ($past(report_valid && !report_ready)) begin
                assert(report_valid && ctl_ready == report_ready);
                assert({report_op, report_class, report_status, report_capacity,
                        report_available, report_inflight, report_queued, report_weight} ==
                       $past({report_op, report_class, report_status, report_capacity,
                              report_available, report_inflight, report_queued, report_weight}));
            end
            if ($past(apply_control)) begin
                assert(report_valid);
                assert({report_op, report_class, report_status} ==
                       $past({ctl_op, ctl_class, status}));
            end
        end
        if (sampled && $past(rst)) begin
            assert(!held && !pending_report);
            assert(cursor == 0 && served_here == 0);
        end
        if (sampled && !rst) begin
            assert(int'(cursor) < CLASSES);
            if (m_valid) begin
                assert(int'(m_class) < CLASSES);
                assert(queued[m_class] != 0);
                assert(m_cost != 0 && m_cost <= available[m_class]);
            end
            if (report_valid) begin
                assert({1'b0, report_available} + {1'b0, report_inflight} ==
                       {1'b0, report_capacity});
                assert(int'(report_queued) <= DEPTH);
            end
            if (refund)
                assert(ctl_amount != 0 && ctl_amount <= inflight[control_index]);
        end
    end

    for (genvar proof_lane = 0; proof_lane < CLASSES; proof_lane++) begin : check_lane
        localparam logic [CLASS_W-1:0] KEY = CLASS_W'(proof_lane);
        wire push_here = push && input_index == KEY;
        wire pop_here = pop && selected == KEY;
        wire refund_here = refund && control_index == KEY;
        wire config_here = configure && control_index == KEY;

        always @(posedge clk) if (sampled) begin
            assert(int'(queued[proof_lane]) <= DEPTH);
            assert(int'(read_ptr[proof_lane]) < DEPTH);
            assert(int'(write_ptr[proof_lane]) < DEPTH);
            assert(weight[proof_lane] != 0);
            assert({1'b0, available[proof_lane]} + {1'b0, inflight[proof_lane]} ==
                   {1'b0, capacity[proof_lane]});
            if ($past(rst)) begin
                assert(queued[proof_lane] == 0);
                assert(capacity[proof_lane] == 0);
            end else begin
                assert(int'(queued[proof_lane]) == $past(int'(queued[proof_lane]))
                       + int'($past(push_here)) - int'($past(pop_here)));
                if (!$past(config_here)) begin
                    assert(capacity[proof_lane] == $past(capacity[proof_lane]));
                    assert(int'(inflight[proof_lane]) == $past(int'(inflight[proof_lane]))
                           + ($past(pop_here) ? $past(int'(m_cost)) : 0)
                           - ($past(refund_here) ? $past(int'(ctl_amount)) : 0));
                end
            end
        end

        always @(posedge clk) if (sampled && !rst) begin
            cover(push_here && pop_here && int'(queued[proof_lane]) == DEPTH);
            cover(refund_here && pop_here);
            cover(refund_here && available[proof_lane] == 0 && queued[proof_lane] != 0);
        end
    end
    always @(posedge clk)
        if (sampled && !rst)
            cover(m_valid && !m_ready && report_valid && !report_ready);
`endif

endmodule

`default_nettype wire
