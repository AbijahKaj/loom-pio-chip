/*
 * Loom — programmable protocol engine (Tiny Tapeout top level).
 * Copyright (c) 2026 AbijahKaj
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module tt_um_abijahkaj_loom (
    input  wire [7:0] ui_in,    // [0]=SCK [1]=CS_N [2]=MOSI [7:3]=GPIO8..12
    output wire [7:0] uo_out,   // [0]=MISO [7:1]=GPIO13..19
    input  wire [7:0] uio_in,   // GPIO0..7
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  localparam [31:0] LOOM_ID = 32'h4C4F_4F4D;  // "LOOM"

  // ------------------------------------------------------------ input synchronisers
  reg [7:0] ui_s1, ui_s2, uio_s1, uio_s2;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      ui_s1 <= 8'h02; ui_s2 <= 8'h02;   // CS_N idles high
      uio_s1 <= 8'h00; uio_s2 <= 8'h00;
    end else begin
      ui_s1 <= ui_in;  ui_s2 <= ui_s1;
      uio_s1 <= uio_in; uio_s2 <= uio_s1;
    end
  end

  // ------------------------------------------------------------ host SPI
  reg         quad;      // HOSTCFG[0]: quad-write mode
  wire [6:0]  h_addr, h_wr_addr;
  wire [31:0] h_wdata;
  wire        h_wr, h_rd_pop;
  reg  [31:0] h_rdata;
  reg         h_rvalid;
  wire        miso;

  loom_spi u_spi (
      .clk(clk), .rst_n(rst_n),
      .sck(ui_s2[0]), .cs_n(ui_s2[1]), .mosi(ui_s2[2]), .miso(miso),
      .quad_en(quad), .qio(ui_s2[5:2]),
      .addr(h_addr), .rdata(h_rdata), .rvalid(h_rvalid), .rd_pop(h_rd_pop),
      .wr(h_wr), .wr_addr(h_wr_addr), .wdata(h_wdata));

  // ------------------------------------------------------------ global registers
  reg  [3:0]  en;
  reg  [3:0]  restart, fifo_clr, div_sync;
  reg  [5:0]  imem_ptr;
  reg  [7:0]  irq;
  reg  [19:0] gpio_out;
  reg  [7:0]  gpio_oe;
  reg  [7:0]  od;
  reg  [15:0] ts;
  reg  [15:0] imem[0:63];

  wire glob_wr = h_wr && (h_wr_addr[6:4] == 3'd0);

  // ------------------------------------------------------------ engines
  wire [19:0] gpio_in = {gpio_out[19:13], ui_s2[7:3], uio_s2};
  wire [5:0]  pc[0:3];
  wire [19:0] po_mask[0:3], po_val[0:3], pd_mask[0:3], pd_val[0:3];
  wire [7:0]  irq_set[0:3], irq_clr[0:3];
  wire [31:0] rxf_head[0:3], dbg[0:3], dbg_x[0:3];
  wire [3:0]  rxf_empty;
  wire [2:0]  rxf_level[0:3], txf_level[0:3];

  genvar g;
  generate
    for (g = 0; g < 4; g = g + 1) begin : eng
      loom_engine #(.ID(g)) u_eng (
          .clk(clk), .rst_n(rst_n),
          .en(en[g]), .restart(restart[g]), .fifo_clr(fifo_clr[g]), .div_sync(div_sync[g]),
          .cfg_we(h_wr && h_wr_addr[6:4] == (g + 1)), .cfg_addr(h_wr_addr[3:0]), .cfg_wdata(h_wdata),
          .rxf_pop(h_rd_pop && h_addr == ((g + 1) * 16 + 5)),
          .rxf_head(rxf_head[g]), .rxf_empty(rxf_empty[g]),
          .rxf_level(rxf_level[g]), .txf_level(txf_level[g]),
          .dbg(dbg[g]), .dbg_x(dbg_x[g]),
          .pc_o(pc[g]), .instr(imem[pc[g]]),
          .gpio_in(gpio_in),
          .po_mask(po_mask[g]), .po_val(po_val[g]), .pd_mask(pd_mask[g]), .pd_val(pd_val[g]),
          .irq_flags(irq), .irq_set(irq_set[g]), .irq_clr(irq_clr[g]),
          .ts(ts));
    end
  endgenerate

  // ------------------------------------------------------------ pin merge (higher engine wins)
  reg [19:0] gpio_out_nx;
  reg [7:0]  gpio_oe_nx;
  reg [7:0]  irq_nx;
  integer e;
  always @(*) begin
    gpio_out_nx = gpio_out;
    gpio_oe_nx  = gpio_oe;
    irq_nx      = irq;
    for (e = 0; e < 4; e = e + 1) begin
      gpio_out_nx = (gpio_out_nx & ~po_mask[e]) | (po_val[e] & po_mask[e]);
      gpio_oe_nx  = (gpio_oe_nx & ~pd_mask[e][7:0]) | (pd_val[e][7:0] & pd_mask[e][7:0]);
    end
    for (e = 0; e < 4; e = e + 1) irq_nx = irq_nx | irq_set[e];
    for (e = 0; e < 4; e = e + 1) irq_nx = irq_nx & ~irq_clr[e];
    if (glob_wr && h_wr_addr[3:0] == 4'h6) irq_nx = (irq_nx & ~h_wdata[7:0]) | h_wdata[15:8];
    if (glob_wr && h_wr_addr[3:0] == 4'h7) gpio_out_nx = h_wdata[19:0];
    if (glob_wr && h_wr_addr[3:0] == 4'h9) gpio_oe_nx = h_wdata[7:0];
  end

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      en       <= 4'd0;
      restart  <= 4'd0;
      fifo_clr <= 4'd0;
      div_sync <= 4'd0;
      imem_ptr <= 6'd0;
      irq      <= 8'd0;
      gpio_out <= 20'd0;
      gpio_oe  <= 8'd0;
      od       <= 8'd0;
      quad     <= 1'b0;
      ts       <= 16'd0;
    end else begin
      ts       <= ts + 16'd1;
      irq      <= irq_nx;
      gpio_out <= gpio_out_nx;
      gpio_oe  <= gpio_oe_nx;
      restart  <= 4'd0;
      fifo_clr <= 4'd0;
      div_sync <= 4'd0;
      if (glob_wr) begin
        case (h_wr_addr[3:0])
          4'h1: begin
            en       <= h_wdata[3:0];
            restart  <= h_wdata[7:4];
            fifo_clr <= h_wdata[11:8];
            div_sync <= h_wdata[15:12];
          end
          4'h3: imem_ptr <= h_wdata[5:0];
          4'h5: imem_ptr <= imem_ptr + 6'd1;
          4'h8: od <= h_wdata[7:0];
          4'hA: quad <= h_wdata[0];
          default: ;
        endcase
      end
    end
  end

  // instruction memory: plain flops, no reset (loaded by the host before enabling)
  always @(posedge clk) begin
    if (glob_wr && h_wr_addr[3:0] == 4'h5) imem[imem_ptr] <= h_wdata[15:0];
  end

  // ------------------------------------------------------------ host read mux
  always @(*) begin
    h_rdata  = 32'd0;
    h_rvalid = 1'b1;
    case (h_addr[6:4])
      3'd0: begin
        case (h_addr[3:0])
          4'h0: h_rdata = LOOM_ID;
          4'h1: h_rdata = {28'd0, en};
          4'h2: h_rdata = {1'b0, rxf_level[3], 1'b0, txf_level[3], 1'b0, rxf_level[2], 1'b0, txf_level[2],
                           1'b0, rxf_level[1], 1'b0, txf_level[1], 1'b0, rxf_level[0], 1'b0, txf_level[0]};
          4'h3: h_rdata = {26'd0, imem_ptr};
          4'h6: h_rdata = {24'd0, irq};
          4'h7: h_rdata = {12'd0, gpio_in};
          4'h8: h_rdata = {24'd0, od};
          4'h9: h_rdata = {24'd0, gpio_oe};
          4'hA: h_rdata = {31'd0, quad};
          default: ;
        endcase
      end
      3'd1, 3'd2, 3'd3, 3'd4: begin
        case (h_addr[3:0])
          4'h5: begin
            h_rvalid = !rxf_empty[h_addr[6:4]-3'd1];
            h_rdata  = h_rvalid ? rxf_head[h_addr[6:4]-3'd1] : 32'd0;
          end
          4'hE: h_rdata = dbg[h_addr[6:4]-3'd1];
          4'hF: h_rdata = dbg_x[h_addr[6:4]-3'd1];
          default: ;
        endcase
      end
      default: ;
    endcase
  end

  // ------------------------------------------------------------ pads
  assign uo_out  = {gpio_out[19:13], miso};
  assign uio_out = gpio_out[7:0] & ~od;
  assign uio_oe  = (gpio_oe & ~od) | (od & ~gpio_out[7:0]);

  wire _unused = &{ena, ui_s2[7:3], 1'b0};
endmodule
