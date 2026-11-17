// Formal properties for loom_fifo: compared against a reference queue model.
// Proven by k-induction (yosys 'sat -tempinduct'), see formal/run.sh.
`default_nettype none
module fifo_props (
    input wire clk,
    input wire rst_n,
    input wire clr,
    input wire push,
    input wire [3:0] wdata,
    input wire pop
);
  localparam AW = 1, D = 2;
  wire [3:0] head;
  wire full, empty;
  wire [AW:0] level;
  loom_fifo #(.WIDTH(4), .AW(AW)) dut (
      .clk(clk), .rst_n(rst_n), .clr(clr), .push(push), .wdata(wdata), .pop(pop),
      .head(head), .full(full), .empty(empty), .level(level));

  // reference model: a 2-entry queue
  reg [3:0] q0, q1;
  reg [1:0] n;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) n <= 0;
    else if (clr) n <= 0;
    else begin
      case ({push && !(n == D), pop && n != 0})
        2'b10: begin if (n == 0) q0 <= wdata; else q1 <= wdata; n <= n + 1; end
        2'b01: begin q0 <= q1; n <= n - 1; end
        2'b11: begin if (n == 1) q0 <= wdata; else begin q0 <= q1; q1 <= wdata; end end
        default: ;
      endcase
    end
  end

  always @(*) begin
    if (rst_n) begin
      assert (level == n);
      assert (empty == (n == 0));
      assert (full == (n == D));
      assert (level <= D);
      if (n != 0) assert (head == q0);
    end
  end
endmodule
