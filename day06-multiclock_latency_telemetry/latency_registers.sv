`timescale 1ns/1ps
`default_nettype none

module latency_registers #(
    parameter integer ADDRESS_W = 12,
    parameter integer PENDING_W = 6,
    parameter integer TRACE_ADDRESS_W = 4,
    parameter integer PROBES = 3,
    parameter integer CLASS_COUNT = 4
) (
    input  wire                      clk,
    input  wire                      reset,
    input  wire                      req_valid,
    output wire                      req_ready,
    input  wire                      req_write,
    input  wire [ADDRESS_W-1:0]      req_addr,
    input  wire [31:0]               req_data,
    input  wire [3:0]                req_strobe,
    output logic                     rsp_valid,
    input  wire                      rsp_ready,
    output logic [31:0]              rsp_data,
    output logic [1:0]               rsp_status,
    input  wire [63:0]               now_tick,
    input  wire [63:0]               now_ns,
    input  wire [31:0]               now_fraction,
    input  wire                      time_saturated,
    input  wire                      invalid_rate,
    input  wire [31:0]               pps_missed,
    input  wire [PENDING_W-1:0]      pending,
    input  wire [31:0]               expired_touch,
    input  wire [255:0]              statistics,
    input  wire [31:0]               completed_snapshots,
    input  wire [31:0]               completed_traces,
    input  wire                      trace_armed,
    input  wire                      trace_frozen,
    input  wire                      histogram_busy,
    output logic                     armed,
    output logic                     flush,
    output logic                     clear_counters,
    output logic                     rate_valid,
    input  wire                      rate_ready,
    output logic [31:0]              rate_ns,
    output logic [31:0]              rate_fraction,
    output logic [63:0]              timeout_ticks,
    output logic                     hist_snapshot,
    input  wire                      hist_snapshot_ready,
    output wire                      trace_arm,
    output logic                     trace_enable,
    output logic [7:0]               trace_mask,
    output logic [63:0]              trace_threshold,
    output logic [TRACE_ADDRESS_W-1:0] trace_post
);
    localparam logic [1:0] OKAY = 2'b00, SLVERR = 2'b10, DECERR = 2'b11;

    logic [31:0] shadow_ns, shadow_fraction;
    logic [63:0] shadow_timeout, shadow_threshold;
    logic [63:0] latched_tick, latched_ns;
    logic [31:0] latched_fraction;

    function automatic logic [31:0] merge_bytes(
        input logic [31:0] old_value,
        input logic [31:0] candidate,
        input logic [3:0] enables
    );
        logic [31:0] mixed;
        begin
            mixed = old_value;
            for (integer byte_no = 0; byte_no < 4; byte_no = byte_no + 1)
                if (enables[byte_no])
                    mixed[8*byte_no +: 8] = candidate[8*byte_no +: 8];
            merge_bytes = mixed;
        end
    endfunction

    wire [31:0] command = merge_bytes(32'd0, req_data, req_strobe);
    wire valid_rate = {shadow_ns, shadow_fraction} != 0;
    wire valid_timeout = shadow_timeout != 0 && !shadow_timeout[63];
    wire [31:0] flags = {24'd0, hist_snapshot, rate_valid, trace_frozen, trace_armed,
                         histogram_busy, invalid_rate, time_saturated, armed};
    wire [31:0] shadow_trace_control =
        {15'd0, trace_enable, 8'(trace_post), trace_mask};
    wire [31:0] edited_trace_control =
        merge_bytes(shadow_trace_control, req_data, req_strobe);
    logic [31:0] read_data;
    logic [1:0] read_result, write_result;

    assign req_ready = !reset && (!rsp_valid || rsp_ready);
    // Arm on the accepted register edge so a concurrent trigger cannot freeze
    // the trace between the readiness check and delivery of the command.
    assign trace_arm = req_valid && req_ready && req_write && req_addr == 'h008 &&
                       write_result == OKAY && command[6];

    always @* begin
        read_data = '0;
        read_result = OKAY;
        write_result = OKAY;
        case (req_addr)
            'h000: begin read_data = 32'h4c41_5431; write_result = SLVERR; end
            'h004: begin read_data = {16'd1, 8'(PROBES), 8'(CLASS_COUNT)}; write_result = SLVERR; end
            'h008: begin
                if (command[1] && hist_snapshot) write_result = SLVERR;
                if (command[4] && (!valid_rate || rate_valid)) write_result = SLVERR;
                if (command[5] && !valid_timeout) write_result = SLVERR;
                if (command[6] && trace_frozen) write_result = SLVERR;
                if (command[31:8] != 0) write_result = SLVERR;
            end
            'h00c: begin read_data = flags; write_result = SLVERR; end
            'h010: read_data = shadow_ns;
            'h014: read_data = shadow_fraction;
            'h018: read_data = shadow_timeout[31:0];
            'h01c: read_data = shadow_timeout[63:32];
            'h020: read_data = shadow_threshold[31:0];
            'h024: read_data = shadow_threshold[63:32];
            'h028: begin
                read_data = shadow_trace_control;
                if (edited_trace_control[31:17] != 0 ||
                    edited_trace_control[15:8] > 8'((1 << TRACE_ADDRESS_W)-1))
                    write_result = SLVERR;
            end
            'h030: begin read_data = latched_tick[31:0]; write_result = SLVERR; end
            'h034: begin read_data = latched_tick[63:32]; write_result = SLVERR; end
            'h038: begin read_data = latched_ns[31:0]; write_result = SLVERR; end
            'h03c: begin read_data = latched_ns[63:32]; write_result = SLVERR; end
            'h040: begin read_data = latched_fraction; write_result = SLVERR; end
            'h044: begin read_data = 32'(pending); write_result = SLVERR; end
            'h048: begin read_data = expired_touch; write_result = SLVERR; end
            'h04c: begin read_data = pps_missed; write_result = SLVERR; end
            'h050: begin read_data = completed_snapshots; write_result = SLVERR; end
            'h054: begin read_data = completed_traces; write_result = SLVERR; end
            'h058: begin read_data = timeout_ticks[31:0]; write_result = SLVERR; end
            'h05c: begin read_data = timeout_ticks[63:32]; write_result = SLVERR; end
            'h080, 'h084, 'h088, 'h08c, 'h090, 'h094, 'h098, 'h09c: begin
                read_data = statistics[32*int'(req_addr[4:2]) +: 32];
                write_result = SLVERR;
            end
            default: begin read_result = DECERR; write_result = DECERR; end
        endcase
    end

    always_ff @(posedge clk) begin
        if (reset) begin
            armed <= 1'b0;
            flush <= 1'b0;
            clear_counters <= 1'b0;
            rsp_valid <= 1'b0;
            rsp_data <= '0;
            rsp_status <= '0;
            shadow_ns <= 8;
            shadow_fraction <= '0;
            shadow_timeout <= 1024;
            timeout_ticks <= 1024;
            shadow_threshold <= '0;
            trace_threshold <= '0;
            trace_enable <= 1'b0;
            trace_post <= '0;
            trace_mask <= 8'hf8;
            hist_snapshot <= 1'b0;
            rate_valid <= 1'b0;
            rate_ns <= '0;
            rate_fraction <= '0;
            latched_tick <= '0;
            latched_ns <= '0;
            latched_fraction <= '0;
        end else begin
            clear_counters <= 1'b0;
            flush <= 1'b0;
            if (rate_valid && rate_ready)
                rate_valid <= 1'b0;
            if (hist_snapshot && hist_snapshot_ready)
                hist_snapshot <= 1'b0;
            if (rsp_valid && rsp_ready)
                rsp_valid <= 1'b0;

            if (req_valid && req_ready) begin
                rsp_valid <= 1'b1;
                rsp_data <= req_write ? '0 : read_data;
                rsp_status <= req_write ? write_result : read_result;
                if (req_write && write_result == OKAY) begin
                    case (req_addr)
                        'h008: begin
                            if (req_strobe[0]) begin
                                if (armed && !command[0])
                                    flush <= 1'b1;
                                armed <= command[0];
                            end
                            if (command[1])
                                hist_snapshot <= 1'b1;
                            if (command[2])
                                clear_counters <= 1'b1;
                            if (command[3])
                                flush <= 1'b1;
                            if (command[4]) begin
                                rate_ns <= shadow_ns;
                                rate_fraction <= shadow_fraction;
                                rate_valid <= 1'b1;
                            end
                            if (command[5])
                                timeout_ticks <= shadow_timeout;
                            if (command[7]) begin
                                latched_tick <= now_tick;
                                latched_ns <= now_ns;
                                latched_fraction <= now_fraction;
                            end
                        end
                        'h010: shadow_ns <= merge_bytes(shadow_ns, req_data, req_strobe);
                        'h014: shadow_fraction <= merge_bytes(shadow_fraction, req_data, req_strobe);
                        'h018: shadow_timeout[31:0] <= merge_bytes(shadow_timeout[31:0], req_data, req_strobe);
                        'h01c: shadow_timeout[63:32] <= merge_bytes(shadow_timeout[63:32], req_data, req_strobe);
                        'h020: begin
                            shadow_threshold[31:0] <= merge_bytes(shadow_threshold[31:0], req_data, req_strobe);
                            trace_threshold[31:0] <= merge_bytes(shadow_threshold[31:0], req_data, req_strobe);
                        end
                        'h024: begin
                            shadow_threshold[63:32] <= merge_bytes(shadow_threshold[63:32], req_data, req_strobe);
                            trace_threshold[63:32] <= merge_bytes(shadow_threshold[63:32], req_data, req_strobe);
                        end
                        'h028: begin
                            trace_enable <= edited_trace_control[16];
                            trace_post <= TRACE_ADDRESS_W'(edited_trace_control[15:8]);
                            trace_mask <= edited_trace_control[7:0];
                        end
                        default: ;
                    endcase
                end
            end
        end
    end
endmodule

`default_nettype wire
