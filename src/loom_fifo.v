/*
 * Loom — synchronous FIFO (power-of-two depth, show-ahead head output).
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module loom_fifo #(
    parameter WIDTH = 32,
    parameter AW    = 2           // depth = 2**AW
) (
    input  wire             clk,
    input  wire             rst_n,
    input  wire             clr,
    input  wire             push,
    input  wire [WIDTH-1:0] wdata,
    input  wire             pop,
    output wire [WIDTH-1:0] head,
    output wire             full,
    output wire             empty,
    output wire [AW:0]      level
);
  reg [WIDTH-1:0] mem[0:(1<<AW)-1];
  reg [AW:0] wp, rp;

  assign level = wp - rp;
  assign empty = (wp == rp);
  assign full  = (level == (1 << AW));
  assign head  = mem[rp[AW-1:0]];

  wire do_push = push && !full;
  wire do_pop  = pop && !empty;

  always @(posedge clk) begin
    if (do_push) mem[wp[AW-1:0]] <= wdata;
  end

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      wp <= 0;
      rp <= 0;
    end else if (clr) begin
      wp <= 0;
      rp <= 0;
    end else begin
      if (do_push) wp <= wp + 1'b1;
      if (do_pop) rp <= rp + 1'b1;
    end
  end
endmodule
