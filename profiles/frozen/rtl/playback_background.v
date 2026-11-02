`ifndef EXP_PLAYBACK_BACKGROUND_V
`define EXP_PLAYBACK_BACKGROUND_V

// KEY11 is transferred as a toggle so a short source-domain event is not lost.
// Requested modes accumulate independently of frame boundaries. Only the
// frame-visible mode changes at frame_start; leaving playback preserves it.
module playback_background (
    input wire clk,
    input wire rst_n,
    input wire frame_start,
    input wire enable,
    input wire cycle_toggle_async,
    input wire [9:0] pixel_x,
    input wire [9:0] pixel_y,
    output reg [1:0] mode_frame = 2'd0,
    output wire [23:0] background_rgb
);

    (* async_reg = "true" *) reg cycle_sync_meta = 1'b0;
    (* async_reg = "true" *) reg cycle_sync_safe = 1'b0;
    reg cycle_sync_history = 1'b0;
    reg [1:0] requested_mode = 2'd0;

    wire cycle_event = cycle_sync_safe ^ cycle_sync_history;
    wire [1:0] requested_mode_next = requested_mode +
        ((enable && cycle_event) ? 2'd1 : 2'd0);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            cycle_sync_meta <= 1'b0;
        else
            cycle_sync_meta <= cycle_toggle_async;
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            cycle_sync_safe <= 1'b0;
        else
            cycle_sync_safe <= cycle_sync_meta;
    end

    // Keep synchronizing while disabled so browse-page key events do not queue.
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            cycle_sync_history <= 1'b0;
        else
            cycle_sync_history <= cycle_sync_safe;
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            requested_mode <= 2'd0;
        else
            requested_mode <= requested_mode_next;
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            mode_frame <= 2'd0;
        else if (frame_start)
            mode_frame <= requested_mode_next;
    end

    // Shifted Manhattan distance gives a soft grayscale falloff without a
    // multiplier or ROM. Six-bit extension keeps addition from truncating.
    wire [9:0] dx = (pixel_x >= 10'd176) ?
        (pixel_x - 10'd176) : (10'd176 - pixel_x);
    wire [9:0] dy = (pixel_y >= 10'd168) ?
        (pixel_y - 10'd168) : (10'd168 - pixel_y);
    wire [5:0] shade = {1'b0, dx[9:5]} + {1'b0, dy[9:5]};
    wire [7:0] gray = 8'd128 - {2'b0, shade};

    // All image modes fall back to the same readable gray while loading.
    assign background_rgb = {gray, gray, gray};

endmodule

`endif
