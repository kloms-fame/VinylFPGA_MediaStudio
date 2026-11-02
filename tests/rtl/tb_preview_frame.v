`timescale 1ns/1ps
module tb_preview_frame;
 reg clk=0; always #5 clk=~clk;
 reg rst_n=0,vs=1,de=0;
 reg [9:0] pixel_x=0;
 reg [23:0] rgb_in=0;
 reg enable,browse_mode,background_image_active;
 reg [1:0] song_id,end_mode;
 reg [4:0] title_length,artist_length;
 reg [127:0] title_glyphs,artist_glyphs,no_lyrics_glyphs;
 reg [7:0] line_count;
 reg lyric_window_valid,lyric_highlight_valid,lyric_scroll_active;
 reg [2:0] lyric_row_base_slot;
 reg [29:0] lyric_row_lengths;
 reg [767:0] lyric_row_glyphs;
 reg [5:0] lyric_scroll_offset;
 reg [8:0] progress_fill;
 reg [39:0] elapsed_time_ascii,total_time_ascii;
 reg [47:0] remaining_time_ascii;
 reg [2:0] brightness,contrast;
 reg wave_enable;
 reg [7:0] wave_values[0:31];
 reg [7:0] wave_peak;
 reg [23:0] frame[0:307199];
 wire [7:0] ascii_row;
 wire [9:0] ascii_addr,out_x;
 wire out_vs,out_de;
 wire [23:0] out_rgb,wave_rgb,adjusted_rgb;
 wire adjusted_de;
 integer fd,px,py,n=0,i;
 ascii_font_rom ascii(.clk(clk),.addr(ascii_addr),.dout(ascii_row));
 playback_page #(.IMAGE_BACKGROUND(1)) page(
   .clk(clk),.rst_n(rst_n),.vs(vs),.de(de),.pixel_x(pixel_x),
   .enable(enable),.browse_mode(browse_mode),.end_mode(end_mode),
   .bg_cycle_toggle_async(1'b0),.background_image_active(background_image_active),
   .background_mode(),.rgb_in(rgb_in),.song_id(song_id),
   .title_length(title_length),.artist_length(artist_length),
   .title_glyphs(title_glyphs),.artist_glyphs(artist_glyphs),.line_count(line_count),
   .lyric_window_valid(lyric_window_valid),.lyric_highlight_valid(lyric_highlight_valid),
   .lyric_scroll_active(lyric_scroll_active),.lyric_row_base_slot(lyric_row_base_slot),
   .lyric_row_lengths(lyric_row_lengths),.lyric_row_glyphs(lyric_row_glyphs),
   .lyric_scroll_offset(lyric_scroll_offset),.no_lyrics_glyphs(no_lyrics_glyphs),
   .progress_fill(progress_fill),.elapsed_time_ascii(elapsed_time_ascii),
   .remaining_time_ascii(remaining_time_ascii),.total_time_ascii(total_time_ascii),
   .ascii_font_row(ascii_row),.ascii_font_addr(ascii_addr),.ascii_font_request(),
   .out_vs(out_vs),.out_de(out_de),.out_pixel_x(out_x),.out_rgb(out_rgb));
 promax_audio_waveform waveform(.clk(clk),.rst_n(rst_n),.vsync(out_vs),.de(out_de),
   .rgb_in(out_rgb),.pixel_x(out_x),.enable(wave_enable),
   .pcm_tap_valid(1'b0),.pcm_tap_left(16'sd0),.rgb_out(wave_rgb),.waveform_active());
 brightness_contrast_steps adjust(.clk(clk),.rst_n(rst_n),.in_de(out_de),.rgb_in(wave_rgb),
   .brightness_level(brightness),.contrast_level(contrast),.out_de(adjusted_de),.rgb_out(adjusted_rgb));
 always @(posedge clk) begin
   #1;
   if(rst_n && adjusted_de) begin
     $fwrite(fd,"%c%c%c",adjusted_rgb[23:16],adjusted_rgb[15:8],adjusted_rgb[7:0]);
     n=n+1;
   end
 end
 initial begin
   `include "config.vh"
   $readmemh("base.hex",frame);
   fd=$fopen("rtl.rgb","wb");
   repeat(4) @(negedge clk);
   rst_n=1;
   waveform.display_valid=1;
   waveform.display_bank=0;
   waveform.display_peak=wave_peak;
   waveform.write_bank=1;
   for(i=0;i<32;i=i+1) waveform.sample_bank0[i]=wave_values[i];
   vs=0;repeat(4) @(negedge clk);vs=1;repeat(4) @(negedge clk);
   for(py=0;py<480;py=py+1) begin
     for(px=0;px<640;px=px+1) begin
       de=1;pixel_x=px;rgb_in=frame[py*640+px];
       @(negedge clk);
     end
     de=0;pixel_x=0;rgb_in=0;repeat(3) @(negedge clk);
   end
   repeat(5) @(negedge clk);
   $fclose(fd);
   if(n!=307200) $fatal(1,"pixel count %0d",n);
   $display("PASS tb_preview_frame: %0d pixels",n);
   $finish;
 end
endmodule
