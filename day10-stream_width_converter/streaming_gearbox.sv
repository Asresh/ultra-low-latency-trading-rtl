`timescale 1ns/1ps
`default_nettype none

// A low-buffer companion to the packet gearbox for already-validated streams.
// Each byte has a tag and final-byte marker. A partial output ends at that marker.
module streaming_gearbox #(
    parameter integer IN_BYTES = 4,
    parameter integer OUT_BYTES = 8,
    parameter integer CAPACITY = IN_BYTES + OUT_BYTES,
    localparam integer PTR_W = $clog2(CAPACITY),
    localparam integer COUNT_W = $clog2(CAPACITY + 1)
) (
    input  wire                     clk,
    input  wire                     rst,
    input  wire                     s_valid,
    output wire                     s_ready,
    input  wire [8*IN_BYTES-1:0]     s_data,
    input  wire [IN_BYTES-1:0]       s_keep,
    input  wire                     s_last,
    input  wire [15:0]              s_tag,
    output wire                     m_valid,
    input  wire                     m_ready,
    output logic [8*OUT_BYTES-1:0]   m_data,
    output logic [OUT_BYTES-1:0]     m_keep,
    output wire                     m_last,
    output wire [15:0]              m_tag,
    output wire [COUNT_W-1:0]        occupancy
);
    logic [23:0] memory [0:CAPACITY-1];
    logic [CAPACITY-1:0] end_marker;
    logic [PTR_W-1:0] read_cursor, write_cursor;
    logic [COUNT_W-1:0] used;
    logic [COUNT_W-1:0] input_count, output_count;
    logic end_visible;
    logic [8*OUT_BYTES-1:0] raw_data;
    logic [OUT_BYTES-1:0] raw_keep;

    // Wrap a cursor plus at most one beat using one conditional subtraction.
    function automatic logic [PTR_W-1:0] step_cursor(
        input logic [PTR_W-1:0] cursor,
        input integer amount
    );
        integer sum;
        begin
            sum = int'(cursor) + amount;
            if (sum >= CAPACITY)
                sum = sum - CAPACITY;
            step_cursor = PTR_W'(sum);
        end
    endfunction

    wire push = s_valid && s_ready;
    wire pop = m_valid && m_ready;
    wire [COUNT_W-1:0] free_bytes = COUNT_W'(CAPACITY) - used;
    assign s_ready = !rst && free_bytes >= input_count && input_count != 0;
    assign m_valid = !rst && (end_visible || output_count == COUNT_W'(OUT_BYTES));
    assign m_last = m_valid && end_visible;
    assign m_data = m_valid ? raw_data : '0;
    assign m_keep = m_valid ? raw_keep : '0;
    assign m_tag = m_valid ? memory[read_cursor][23:8] : '0;
    assign occupancy = used;

    // A legal input provides a contiguous low-lane keep.
    always_comb begin
        input_count = '0;
        for (integer lane = 0; lane < IN_BYTES; lane++)
            input_count = input_count + COUNT_W'(s_keep[lane]);
    end

    // Inspect only bytes which are present. Never cross a packet boundary.
    always @* begin : gather
        logic [PTR_W-1:0] index;
        output_count = '0;
        end_visible = 0;
        raw_data = '0;
        raw_keep = '0;
        index = '0;
        for (integer lane = 0; lane < OUT_BYTES; lane++) begin
            if (COUNT_W'(lane) < used && !end_visible) begin
                index = step_cursor(read_cursor, lane);
                raw_data[8*lane+:8] = memory[index][7:0];
                raw_keep[lane] = 1;
                end_visible = end_marker[index];
                output_count = COUNT_W'(lane + 1);
            end
        end
    end

    always_ff @(posedge clk) begin
        if (push) begin
            for (integer lane = 0; lane < IN_BYTES; lane++)
                if (s_keep[lane]) begin
                    memory[step_cursor(write_cursor, lane)] <= {s_tag,s_data[8*lane+:8]};
                    end_marker[step_cursor(write_cursor, lane)] <=
                        s_last && input_count == COUNT_W'(lane+1);
                end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            used <= '0;
            read_cursor <= '0;
            write_cursor <= '0;
        end else begin
            if (push)
                write_cursor <= step_cursor(write_cursor, int'(input_count));
            if (pop)
                read_cursor <= step_cursor(read_cursor, int'(output_count));
            used <= used + (push ? input_count : COUNT_W'(0)) -
                           (pop ? output_count : COUNT_W'(0));
        end
    end
endmodule

`default_nettype wire
