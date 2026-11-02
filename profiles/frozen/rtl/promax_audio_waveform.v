`timescale 1ns/1ps

// 第十二讲音频波形可视化。
//
// 显示形式：32 根从底部向上生长的柱，右侧保留一根独立的峰值柱。
// 该模块只替换 RGB 像素，不改变 de/vs 节拍，也不参与音频数据通路。
module promax_audio_waveform #(
    parameter integer WAVE_X = 64,
    parameter integer WAVE_Y = 376,
    parameter integer WAVE_WIDTH = 512,
    parameter integer WAVE_HEIGHT = 96,
    parameter integer DECIMATE = 12
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        vsync,
    input  wire        de,
    input  wire [23:0] rgb_in,
    input  wire [9:0]  pixel_x,
    input  wire        enable,
    input  wire        pcm_tap_valid,
    input  wire signed [15:0] pcm_tap_left,
    output wire [23:0] rgb_out,
    output wire        waveform_active
);
    localparam integer SAMPLE_COUNT   = 32;
    localparam integer SAMPLE_SPACING = WAVE_WIDTH / SAMPLE_COUNT;
    localparam integer DISPLAY_HEIGHT = WAVE_HEIGHT - 1;
    localparam integer BASE_Y         = WAVE_Y + WAVE_HEIGHT - 1;

    // 512 像素分成 32 个 16 像素槽位；中间 12 像素画柱，左右各留 2 像素。
    localparam integer SLOT_SHIFT = 4;
    localparam integer BAR_GAP    = 2;
    localparam integer BAR_WIDTH  = 12;

    reg [11:0] line_y;
    reg        de_q;
    reg        vs_q;
    wire       frame_start = vs_q && !vsync;

    // 双缓冲：采集当前帧，显示上一帧，避免柱高和音频节拍互相撕裂。
    reg [7:0] sample_bank0 [0:SAMPLE_COUNT-1];
    reg [7:0] sample_bank1 [0:SAMPLE_COUNT-1];
    reg       write_bank;
    reg       display_bank;
    reg       display_valid;
    reg [5:0] capture_count;
    reg [7:0] decimate_count;
    reg [7:0] peak_bank0;
    reg [7:0] peak_bank1;
    reg [7:0] display_peak;

    // 取 16bit PCM 的高 8bit 绝对值，保留原始音量差异。
    wire [7:0] sample_abs = pcm_tap_left[15] ?
                            (~pcm_tap_left[15:8] + 1'b1) :
                            pcm_tap_left[15:8];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            line_y         <= 12'd0;
            de_q           <= 1'b0;
            vs_q           <= 1'b1;
            write_bank     <= 1'b0;
            display_bank   <= 1'b1;
            display_valid  <= 1'b0;
            capture_count  <= 6'd0;
            decimate_count <= 8'd0;
            peak_bank0     <= 8'd0;
            peak_bank1     <= 8'd0;
            display_peak   <= 8'd0;
        end else begin
            de_q <= de;
            vs_q <= vsync;

            if (!vsync)
                line_y <= 12'd0;
            else if (de_q && !de)
                line_y <= line_y + 1'b1;

            if (frame_start) begin
                if (capture_count == SAMPLE_COUNT) begin
                    display_bank  <= write_bank;
                    display_valid <= 1'b1;
                    display_peak  <= write_bank ? peak_bank1 : peak_bank0;
                    write_bank    <= ~write_bank;
                end

                capture_count  <= 6'd0;
                decimate_count <= 8'd0;
                if (write_bank)
                    peak_bank0 <= 8'd0;
                else
                    peak_bank1 <= 8'd0;
            end else if (pcm_tap_valid && capture_count < SAMPLE_COUNT) begin
                if (decimate_count == 0) begin
                    if (write_bank)
                        sample_bank1[capture_count] <= sample_abs;
                    else
                        sample_bank0[capture_count] <= sample_abs;

                    capture_count  <= capture_count + 1'b1;
                    decimate_count <= DECIMATE - 1;

                    if (write_bank) begin
                        if (sample_abs > peak_bank1)
                            peak_bank1 <= sample_abs;
                    end else begin
                        if (sample_abs > peak_bank0)
                            peak_bank0 <= sample_abs;
                    end
                end else begin
                    decimate_count <= decimate_count - 1'b1;
                end
            end
        end
    end

    reg [4:0] sample_index;
    reg [4:0] slot_pixel;
    reg [6:0] row_from_base;
    reg [6:0] bar_height;
    reg [6:0] peak_height;
    reg [7:0] selected_sample;
    reg       in_wave_area;
    reg       in_peak_area;
    reg       bar_pixel;
    reg       bar_top_pixel;
    reg       baseline_pixel;
    reg       peak_pixel;
    reg [23:0] bar_color;
    reg [23:0] peak_color;

    always @* begin
        sample_index    = 5'd0;
        slot_pixel      = 5'd0;
        row_from_base   = 7'd0;
        bar_height      = 7'd0;
        peak_height     = 7'd0;
        selected_sample = 8'd0;
        in_wave_area    = 1'b0;
        in_peak_area    = 1'b0;
        bar_pixel       = 1'b0;
        bar_top_pixel   = 1'b0;
        baseline_pixel  = 1'b0;
        peak_pixel      = 1'b0;
        bar_color       = 24'h3070ff;
        peak_color      = 24'h00d8ff;

        in_wave_area = enable && de &&
                       pixel_x >= WAVE_X &&
                       pixel_x < WAVE_X + WAVE_WIDTH &&
                       line_y >= WAVE_Y &&
                       line_y <= BASE_Y;
        in_peak_area = enable && de &&
                       pixel_x >= WAVE_X + WAVE_WIDTH + 8 &&
                       pixel_x < WAVE_X + WAVE_WIDTH + 20 &&
                       line_y >= WAVE_Y &&
                       line_y <= BASE_Y;

        if (in_wave_area) begin
            row_from_base = BASE_Y - line_y;
            sample_index  = (pixel_x - WAVE_X) >> SLOT_SHIFT;
            slot_pixel    = (pixel_x - WAVE_X) & 5'h0f;

            // 在底部画一条 2 像素基准线，柱只从基准线向上生长。
            baseline_pixel = (row_from_base <= 1);

            if (display_valid &&
                slot_pixel >= BAR_GAP &&
                slot_pixel < BAR_GAP + BAR_WIDTH) begin
                selected_sample = display_bank ?
                                  sample_bank1[sample_index] :
                                  sample_bank0[sample_index];

                // 使用 16bit PCM 的高 8bit 直接映射，96 像素窗口只做上限保护。
                // 高幅度时更快接近满高，同时保留不同采样值之间的明显高低差。
                // Clamp at the source width before narrowing to seven bits.
                if (selected_sample > DISPLAY_HEIGHT)
                    bar_height = DISPLAY_HEIGHT;
                else
                    bar_height = selected_sample[6:0];
                if (bar_height == 0)
                    bar_height = 1;

                bar_pixel     = (row_from_base <= bar_height);
                bar_top_pixel = (row_from_base == bar_height);

                // 32 根柱按相邻 4 根一组轮换色相，形成清晰但不会过亮刺眼的彩色波形。
                case (sample_index[4:2])
                    3'd0: bar_color = 24'hff3040; // 红
                    3'd1: bar_color = 24'hff8a20; // 橙
                    3'd2: bar_color = 24'hffe040; // 黄
                    3'd3: bar_color = 24'h38e060; // 绿
                    3'd4: bar_color = 24'h18d8c0; // 青绿
                    3'd5: bar_color = 24'h20a0ff; // 蓝
                    3'd6: bar_color = 24'h7060ff; // 紫
                    default: bar_color = 24'hff50d0; // 洋红
                endcase
            end
        end

        if (in_peak_area) begin
            row_from_base = BASE_Y - line_y;
            if (display_peak > DISPLAY_HEIGHT)
                peak_height = DISPLAY_HEIGHT;
            else
                peak_height = display_peak[6:0];
            if (peak_height == 0)
                peak_height = 1;
            peak_pixel = (row_from_base <= peak_height);

            if (row_from_base > (peak_height - (peak_height >> 2)))
                peak_color = 24'hffffff;
            else if (row_from_base > (peak_height >> 1))
                peak_color = 24'hffb020;
            else
                peak_color = 24'h00d8ff;
        end
    end

    assign waveform_active = bar_pixel || bar_top_pixel ||
                             baseline_pixel || peak_pixel;
    assign rgb_out = bar_top_pixel ? 24'hfff8d0 :
                     bar_pixel     ? bar_color :
                     peak_pixel    ? peak_color :
                     baseline_pixel ? 24'h406060 :
                                      rgb_in;
endmodule
