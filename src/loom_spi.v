/*
 * Loom — host SPI target (mode 0, oversampled; SCK <= clk/8).
 *
 * Transaction: CS_N low, command byte {rw, addr[6:0]} (rw=1 read), then 4 bytes per
 * 32-bit word, little-endian byte order, MSB-first bit order within each byte.
 * The address auto-increments after each word, except for "port" addresses
 * (IMEM_DATA, engine TXF/RXF), so FIFOs can be streamed.
 *
 * Reads peek the register at the start of each word and only commit a pop
 * (rd_pop) when the host actually starts clocking that word, so a trailing
 * read-ahead never loses FIFO data.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module loom_spi (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        sck,      // already synchronised
    input  wire        cs_n,     // already synchronised
    input  wire        mosi,     // already synchronised
    output wire        miso,
    // register bus
    output reg  [6:0]  addr,     // current read address (combinational read mux)
    input  wire [31:0] rdata,
    input  wire        rvalid,   // read data is real (e.g. FIFO not empty)
    output reg         rd_pop,   // commit a pop of the word at `addr`
    output reg         wr,
    output reg  [6:0]  wr_addr,
    output reg  [31:0] wdata
);
  reg       sck_d;
  reg [2:0] bitcnt;
  reg [6:0] rx;
  reg [7:0] tx;
  reg       have_cmd;
  reg       rw;
  reg [1:0] k;
  reg       rd_req;
  reg [31:0] rword;
  reg       rword_valid;

  wire rise = sck & ~sck_d;
  wire fall = ~sck & sck_d;
  wire [7:0] byte_in = {rx, mosi};

  function is_port(input [6:0] a);
    is_port = (a == 7'h05) || ((a[6:4] != 3'd0) && ((a[3:0] == 4'h4) || (a[3:0] == 4'h5)));
  endfunction

  assign miso = tx[7];

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      sck_d       <= 1'b0;
      bitcnt      <= 3'd0;
      rx          <= 7'd0;
      tx          <= 8'd0;
      have_cmd    <= 1'b0;
      rw          <= 1'b0;
      k           <= 2'd0;
      rd_req      <= 1'b0;
      rword       <= 32'd0;
      rword_valid <= 1'b0;
      addr        <= 7'd0;
      rd_pop      <= 1'b0;
      wr          <= 1'b0;
      wr_addr     <= 7'd0;
      wdata       <= 32'd0;
    end else begin
      sck_d  <= sck;
      wr     <= 1'b0;
      rd_pop <= 1'b0;
      rd_req <= 1'b0;
      if (rd_req) begin
        rword       <= rdata;
        rword_valid <= rvalid;
      end
      if (cs_n) begin
        bitcnt   <= 3'd0;
        have_cmd <= 1'b0;
        k        <= 2'd0;
        tx       <= 8'd0;
      end else begin
        if (rise) begin
          rx     <= byte_in[6:0];
          bitcnt <= bitcnt + 3'd1;
          if (bitcnt == 3'd0 && have_cmd && rw && k == 2'd0 && rword_valid) rd_pop <= 1'b1;
          if (bitcnt == 3'd7) begin
            if (!have_cmd) begin
              have_cmd <= 1'b1;
              rw       <= byte_in[7];
              addr     <= byte_in[6:0];
              k        <= 2'd0;
              rd_req   <= byte_in[7];
            end else begin
              k <= k + 2'd1;
              if (!rw) wdata <= {byte_in, wdata[31:8]};
              if (k == 2'd3) begin
                if (!rw) begin
                  wr      <= 1'b1;
                  wr_addr <= addr;
                end else begin
                  rd_req <= 1'b1;
                end
                if (!is_port(addr)) addr <= addr + 7'd1;
              end
            end
          end
        end
        if (fall) begin
          if (bitcnt == 3'd0) tx <= (have_cmd && rw) ? rword[{k, 3'b000}+:8] : 8'd0;
          else tx <= {tx[6:0], 1'b0};
        end
      end
    end
  end
endmodule
