// Assigns a whole row of a two-dimensional unpacked array, as XLS's
// SystemVerilog does for arrays of arrays. Yosys's own reader rejects it
// ("Insufficient number of array indices"); sv2v converts it.
module arr2 (
  input  wire [31:0] a,
  input  wire        clk,
  output reg  [7:0]  o
);
  wire [7:0] row [0:3];
  wire [7:0] blk [0:1][0:3];
  assign row[0] = a[31:24];
  assign row[1] = a[23:16];
  assign row[2] = a[15:8];
  assign row[3] = a[7:0];
  assign blk[0] = row;
  assign blk[1] = row;
  always @(posedge clk) o <= blk[1][2] ^ blk[0][0];
endmodule
