// A 4-bit counter with an enable.
module count (
  input  wire       clk,
  input  wire       rst,
  input  wire       en,
  output reg  [3:0] q
);
`ifdef COUNT_DOWN
  always @(posedge clk) q <= rst ? 4'd0 : en ? q - 4'd1 : q;
`else
  always @(posedge clk) q <= rst ? 4'd0 : en ? q + 4'd1 : q;
`endif
endmodule
