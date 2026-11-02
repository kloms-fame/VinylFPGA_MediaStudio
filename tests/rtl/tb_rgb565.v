`timescale 1ns/1ps
// Exercise the actual mv_stream_player combinational decoder for every value.
module tb_rgb565;
 reg clk=0;always #5 clk=~clk;
 reg [7:0] sector_byte=0;
 wire [31:0] pixel_data;
 integer fd,i;
 mv_stream_player decoder(.clk(clk),.rst_n(1'b1),.abort_request(1'b0),
 .request_toggle(1'b0),.request_lba(32'd0),.request_bank(2'd0),
 .done_toggle(),.failed(),.writer_active(),.write_req(),.write_ack(1'b0),
 .write_finish_toggle(1'b0),.write_bank(),.pixel_valid(),.pixel_data(pixel_data),
 .sector_req(),.sector_lba(),.sector_busy(1'b0),.sector_valid(1'b0),
 .sector_byte(sector_byte),.sector_byte_index(9'd0),.sector_done(1'b0),.sector_error(8'd0));
 initial begin
  fd=$fopen("rgb565.rgb","wb");
  for(i=0;i<65536;i=i+1) begin
   decoder.low_byte=i[7:0];sector_byte=i[15:8];#1;
   $fwrite(fd,"%c%c%c",pixel_data[31:24],pixel_data[23:16],pixel_data[15:8]);
  end
  $fclose(fd);$display("PASS tb_rgb565: all 65536 values");$finish;
 end
endmodule
