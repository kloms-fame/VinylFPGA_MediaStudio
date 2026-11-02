`timescale 1ns/1ps
`ifndef EXP_LRC_FIVE_OSD_V
`define EXP_LRC_FIVE_OSD_V
`include "playback_layout.vh"

// D11/D12 pixel selector for the five visible rows and sixth incoming row.
// This block selects a glyph request only; playback_page arbitrates the
// request with title/artist/time and drives the single shared font ROM.
module lrc_five_osd (
    input  wire          enable,
    input  wire          de,
    input  wire [9:0]    pixel_x,
    input  wire [9:0]    pixel_y,
    input  wire          window_valid,
    input  wire          highlight_valid,
    input  wire          scroll_active,
    input  wire [2:0]    row_base_slot,
    input  wire [29:0]   row_lengths,
    input  wire [767:0]  row_glyphs,
    input  wire [5:0]    scroll_offset,
    input  wire [7:0]    line_count,
    input  wire [127:0] no_lyrics_glyphs,
    output reg           font_request,
    output reg  [7:0]    font_glyph,
    output reg  [3:0]    font_column,
    output reg  [3:0]    font_pixel_column,
    output reg  [3:0]    font_row,
    output reg  [23:0]   text_color
);
    wire [9:0] lyric_dx = pixel_x - `PLAYBACK_LYRIC_X;
    wire lyric_x_active = (pixel_x >= `PLAYBACK_LYRIC_X) &&
        (pixel_x < (`PLAYBACK_LYRIC_X + 10'd256));
    wire lyric_y_active = (pixel_y >= `PLAYBACK_INFO_Y0) &&
        (pixel_y < `PLAYBACK_INFO_Y1);

    wire [10:0] lyric_y_sum = {1'b0, pixel_y} + {5'd0, scroll_offset};
    wire [5:0] lyric_row_phase = lyric_y_sum[9:4] - 6'd8;
    reg [2:0] selected_slot;
    reg [2:0] selected_physical_slot;
    reg [3:0] selected_slot_sum;
    reg selected_row_found;
    reg [3:0] selected_row_pixel;
    reg [4:0] selected_length;
    reg [7:0] selected_glyph;

    function [7:0] glyph_byte_at;
        input [127:0] glyphs;
        input [3:0] column;
        begin
            case (column)
                4'd0: glyph_byte_at = glyphs[7:0];
                4'd1: glyph_byte_at = glyphs[15:8];
                4'd2: glyph_byte_at = glyphs[23:16];
                4'd3: glyph_byte_at = glyphs[31:24];
                4'd4: glyph_byte_at = glyphs[39:32];
                4'd5: glyph_byte_at = glyphs[47:40];
                4'd6: glyph_byte_at = glyphs[55:48];
                4'd7: glyph_byte_at = glyphs[63:56];
                4'd8: glyph_byte_at = glyphs[71:64];
                4'd9: glyph_byte_at = glyphs[79:72];
                4'd10: glyph_byte_at = glyphs[87:80];
                4'd11: glyph_byte_at = glyphs[95:88];
                4'd12: glyph_byte_at = glyphs[103:96];
                4'd13: glyph_byte_at = glyphs[111:104];
                4'd14: glyph_byte_at = glyphs[119:112];
                default: glyph_byte_at = glyphs[127:120];
            endcase
        end
    endfunction

    always @* begin
        font_request = 1'b0;
        font_glyph = 8'd0;
        font_column = lyric_dx[7:4];
        font_pixel_column = lyric_dx[3:0];
        font_row = 4'd0;
        text_color = 24'hFFFFFF;
        selected_slot = 3'd0;
        selected_physical_slot = 3'd0;
        selected_slot_sum = 4'd0;
        selected_row_found = 1'b0;
        selected_row_pixel = 4'd0;
        selected_length = 5'd0;
        selected_glyph = 8'd0;

        if (enable && de && lyric_x_active && lyric_y_active) begin
            if (line_count == 0) begin
                if ((pixel_y >= `PLAYBACK_LYRIC_Y) &&
                    (pixel_y < (`PLAYBACK_LYRIC_Y + 10'd16)) &&
                    (font_column < 4'd9)) begin
                    font_request = 1'b1;
                    font_glyph = glyph_byte_at(no_lyrics_glyphs, font_column);
                    font_row = pixel_y - `PLAYBACK_LYRIC_Y;
                end
            end else if (window_valid) begin
                // Rows start every 48 pixels at y=128-scroll_offset.  After
                // folding the offset into y, each glyph occupies phase 0..15
                // of row groups 0,3,6,9,12,15 (16-pixel units).
                if (lyric_y_sum >= 11'd128) begin
                    case (lyric_row_phase)
                        6'd0:  begin selected_slot = 3'd0; selected_row_found = 1'b1; end
                        6'd3:  begin selected_slot = 3'd1; selected_row_found = 1'b1; end
                        6'd6:  begin selected_slot = 3'd2; selected_row_found = 1'b1; end
                        6'd9:  begin selected_slot = 3'd3; selected_row_found = 1'b1; end
                        6'd12: begin selected_slot = 3'd4; selected_row_found = 1'b1; end
                        6'd15: begin selected_slot = 3'd5; selected_row_found = 1'b1; end
                        default: ;
                    endcase
                    selected_row_pixel = lyric_y_sum[3:0];
                end

                if (selected_row_found) begin
                    selected_slot_sum = {1'b0, selected_slot} + {1'b0, row_base_slot};
                    selected_physical_slot = (selected_slot_sum >= 4'd6)
                                           ? selected_slot_sum - 4'd6
                                           : selected_slot_sum[2:0];
                    case (selected_physical_slot)
                        3'd0: begin selected_length = row_lengths[4:0];   selected_glyph = glyph_byte_at(row_glyphs[127:0], font_column); end
                        3'd1: begin selected_length = row_lengths[9:5];   selected_glyph = glyph_byte_at(row_glyphs[255:128], font_column); end
                        3'd2: begin selected_length = row_lengths[14:10]; selected_glyph = glyph_byte_at(row_glyphs[383:256], font_column); end
                        3'd3: begin selected_length = row_lengths[19:15]; selected_glyph = glyph_byte_at(row_glyphs[511:384], font_column); end
                        3'd4: begin selected_length = row_lengths[24:20]; selected_glyph = glyph_byte_at(row_glyphs[639:512], font_column); end
                        default: begin selected_length = row_lengths[29:25]; selected_glyph = glyph_byte_at(row_glyphs[767:640], font_column); end
                    endcase
                    if (font_column < selected_length) begin
                        font_request = 1'b1;
                        font_glyph = selected_glyph;
                        font_row = selected_row_pixel;
                        if (highlight_valid &&
                            ((scroll_active && (selected_slot == 3'd3)) ||
                             (!scroll_active && (selected_slot == 3'd2))))
                            text_color = 24'hFFD700;
                        else if ((!scroll_active &&
                                  ((selected_slot == 3'd1) || (selected_slot == 3'd3))) ||
                                 (scroll_active &&
                                  ((selected_slot == 3'd2) || (selected_slot == 3'd4))))
                            text_color = 24'h606068;
                        else
                            text_color = 24'h606068;
                    end
                end
            end
        end
    end
endmodule
`endif
