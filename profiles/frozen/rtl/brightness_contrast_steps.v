`timescale 1ns/1ps
`ifndef EXP_BRIGHTNESS_CONTRAST_STEPS_V
`define EXP_BRIGHTNESS_CONTRAST_STEPS_V
// D4/D5: five constant brightness and contrast levels; one registered pixel stage.
module brightness_contrast_steps (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        in_de,
    input  wire [23:0] rgb_in,
    input  wire [2:0]  brightness_level, // 1/4, 1/2, 3/4, 1, 5/4
    input  wire [2:0]  contrast_level,   // 1/2, 3/4, 1, 5/4, 3/2
    output reg         out_de,
    output reg  [23:0] rgb_out
);
    function [7:0] adjust_channel;
        input [7:0] pixel;
        input [2:0] brightness_sel;
        input [2:0] contrast_sel;
        reg [10:0] pixel_ext;
        reg [10:0] bright_ext;
        reg [7:0] bright;
        reg signed [10:0] delta;
        reg signed [12:0] delta_ext;
        reg signed [12:0] contrast_sum;
        reg signed [12:0] contrast_value;
        reg signed [12:0] result_value;
        begin
            pixel_ext = {3'b000, pixel};
            case (brightness_sel)
                3'd0: bright_ext = pixel_ext >> 2;
                3'd1: bright_ext = pixel_ext >> 1;
                3'd2: bright_ext = (pixel_ext + (pixel_ext << 1)) >> 2;
                3'd3: bright_ext = pixel_ext;
                3'd4: bright_ext = (pixel_ext + (pixel_ext << 2)) >> 2;
                default: bright_ext = pixel_ext;
            endcase
            bright = (bright_ext > 11'd255) ? 8'd255 : bright_ext[7:0];

            delta = $signed({1'b0, bright}) - 11'sd128;
            delta_ext = {{2{delta[10]}}, delta};
            case (contrast_sel)
                3'd0: contrast_value = delta_ext >>> 1;
                3'd1: begin
                    contrast_sum = delta_ext + (delta_ext <<< 1);
                    contrast_value = contrast_sum >>> 2;
                end
                3'd2: contrast_value = delta_ext;
                3'd3: begin
                    contrast_sum = delta_ext + (delta_ext <<< 2);
                    contrast_value = contrast_sum >>> 2;
                end
                3'd4: begin
                    contrast_sum = delta_ext + (delta_ext <<< 1);
                    contrast_value = contrast_sum >>> 1;
                end
                default: contrast_value = delta_ext;
            endcase
            result_value = contrast_value + 13'sd128;
            if (result_value < 13'sd0)
                adjust_channel = 8'd0;
            else if (result_value > 13'sd255)
                adjust_channel = 8'd255;
            else
                adjust_channel = result_value[7:0];
        end
    endfunction

    wire [7:0] red_adj   = adjust_channel(rgb_in[23:16], brightness_level, contrast_level);
    wire [7:0] green_adj = adjust_channel(rgb_in[15:8],  brightness_level, contrast_level);
    wire [7:0] blue_adj  = adjust_channel(rgb_in[7:0],   brightness_level, contrast_level);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            out_de  <= 1'b0;
            rgb_out <= 24'h000000;
        end else begin
            out_de  <= in_de;
            rgb_out <= {red_adj, green_adj, blue_adj};
        end
    end
endmodule
`endif
