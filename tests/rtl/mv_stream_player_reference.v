`ifndef VINYL_MV_STREAM_PLAYER_V
`define VINYL_MV_STREAM_PLAYER_V
// sd_card_clk: fetch exactly one frame (300 sectors), expand RGB565 LE to
// the existing {RGB888,8'b0} write FIFO. Completion means SDRAM write_finish,
// never merely the last SD byte. Command data stays stable until done_toggle.
module mv_stream_player #(
 parameter integer FRAME_SECTORS=300
)(
 input wire clk, rst_n,
 input wire abort_request,
 input wire request_toggle, input wire [31:0] request_lba,
 input wire [1:0] request_bank,
 output reg done_toggle, output reg failed,
 output wire writer_active, output wire write_req,
 input wire write_ack, input wire write_finish_toggle,
 output reg [1:0] write_bank,
 output wire pixel_valid, output wire [31:0] pixel_data,
 output wire sector_req, output reg [31:0] sector_lba,
 input wire sector_busy, sector_valid, input wire [7:0] sector_byte,
 input wire [8:0] sector_byte_index,
 input wire sector_done, input wire [7:0] sector_error
);
 localparam IDLE=3'd0, ACK=3'd1, CLEAR=3'd2, REQ=3'd3,
            READ=3'd4, DRAIN=3'd5, COMPLETE=3'd6;
 reg [2:0] state;
 reg [2:0] req_sync, ack_sync, finish_sync;
 reg request_seen;
 reg [8:0] sector_count;
 reg [9:0] byte_count;
 reg [7:0] low_byte;
 reg finish_mark;
 wire [15:0] rgb565 = {sector_byte,low_byte};
 assign pixel_data = {rgb565[15:11],rgb565[15:13],rgb565[10:5],rgb565[10:9],
                      rgb565[4:0],rgb565[4:2],8'd0};
 assign writer_active = (state != IDLE);
 assign write_req = (state == ACK);
 // Hold demand until the arbiter accepts it via !sector_busy. Gating demand
 // with busy hides it from weighted arbitration and can starve video forever.
 assign sector_req = (state == REQ) && !abort_request;
 assign pixel_valid = (state == READ) && sector_valid && sector_byte_index[0] && !failed;
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) req_sync<=0; else req_sync<={req_sync[1:0],request_toggle};
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) ack_sync<=0; else ack_sync<={ack_sync[1:0],write_ack};
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) finish_sync<=0; else finish_sync<={finish_sync[1:0],write_finish_toggle};
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) request_seen<=0;
  else if(state==IDLE && req_sync[2]!=request_seen) request_seen<=req_sync[2];
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) state<=IDLE;
  else case(state)
   IDLE: if(req_sync[2]!=request_seen) state<=ACK;
   ACK: if(ack_sync[2]) state<=CLEAR;
   CLEAR: if(!ack_sync[2]) state<=REQ;
   REQ: if(abort_request) state<=COMPLETE; else if(!sector_busy) state<=READ;
   READ: if(sector_done) begin
    if(abort_request || sector_error!=0 || byte_count!=10'd512) state<=COMPLETE;
    else if(sector_count==FRAME_SECTORS-1) state<=DRAIN;
    else state<=REQ;
   end
   DRAIN: if(abort_request || finish_sync[2]!=finish_mark) state<=COMPLETE;
   COMPLETE: state<=IDLE;
   default: state<=IDLE;
  endcase
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) done_toggle<=0;
  else if(state==COMPLETE) done_toggle<=request_seen;
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) failed<=0;
  else if(state==IDLE && req_sync[2]!=request_seen) failed<=0;
  else if(abort_request && state!=IDLE) failed<=1;
  else if(state==READ && sector_done && (sector_error!=0 || byte_count!=10'd512)) failed<=1;
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) finish_mark<=0;
  else if(state==CLEAR) finish_mark<=finish_sync[2];
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) write_bank<=0;
  else if(state==IDLE && req_sync[2]!=request_seen) write_bank<=request_bank;
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) sector_lba<=0;
  else if(state==IDLE && req_sync[2]!=request_seen) sector_lba<=request_lba;
  else if(state==READ && sector_done) sector_lba<=sector_lba+1'b1;
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) sector_count<=0;
  else if(state==IDLE) sector_count<=0;
  else if(state==READ && sector_done) sector_count<=sector_count+1'b1;
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) byte_count<=0;
  else if(state==REQ) byte_count<=0;
  else if(state==READ && sector_valid) byte_count<=byte_count+1'b1;
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) low_byte<=0;
  else if(state==READ && sector_valid && !sector_byte_index[0]) low_byte<=sector_byte;
 end
endmodule
`endif
