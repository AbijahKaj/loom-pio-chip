/*
 * Loom — bit engine: a PIO-style state machine plus line helpers
 * (bit-stuffing, NRZI, Manchester, programmable CRC, edge timestamps,
 * DPLL clock recovery, pattern events and event traps).
 *
 * See docs/architecture.md for the ISA and exact semantics; tools/loom/sim.py
 * is the cycle-exact golden model of this file.
 *
 * Area notes: all pin writes share one rotate unit, and IN/OUT share one
 * 64->32 funnel shifter and one left shifter.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module loom_engine #(
    parameter [1:0] ID      = 2'd0,
    parameter       FIFO_AW = 1         // FIFO depth = 2**FIFO_AW
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        en,
    input  wire        restart,     // pulse
    input  wire        fifo_clr,    // pulse
    input  wire        div_sync,    // pulse
    // host config bus
    input  wire        cfg_we,
    input  wire [3:0]  cfg_addr,
    input  wire [31:0] cfg_wdata,
    input  wire        rxf_pop,
    output wire [31:0] rxf_head,
    output wire        rxf_empty,
    output wire [2:0]  rxf_level,
    output wire [2:0]  txf_level,
    output wire [31:0] dbg,
    output wire [31:0] dbg_x,
    // instruction memory
    output wire [5:0]  pc_o,
    input  wire [15:0] instr,
    // pins
    input  wire [19:0] gpio_in,
    output wire [19:0] po_mask,
    output wire [19:0] po_val,
    output wire [19:0] pd_mask,
    output wire [19:0] pd_val,
    // shared IRQ flags
    input  wire [7:0]  irq_flags,
    output reg  [7:0]  irq_set,
    output reg  [7:0]  irq_clr,
    // global timestamp counter
    input  wire [15:0] ts
);
  // ---------------------------------------------------------------- config
  reg [15:0] div_int;
  reg [7:0]  div_frac;
  reg [5:0]  wrap_bot, wrap_top, start_addr;
  reg [2:0]  ss_count;
  reg        ss_en, ss_pindir;
  reg [4:0]  jmp_pin;
  reg        in_shr, out_shr, autopush, autopull;
  reg [4:0]  push_th, pull_th;
  reg [4:0]  out_base, set_base, side_base, in_base;
  reg [5:0]  out_count;
  reg [2:0]  set_count;
  reg [1:0]  c_mode;
  reg        c_nrzi_tog, c_manch_pol, c_stuff_en, c_stuff_mode, c_diff, c_idle;
  reg [3:0]  c_stuff_n;
  reg [4:0]  ts_pin;
  reg [1:0]  ts_edge;
  reg        dpll_en;
  reg [5:0]  crc_width;
  reg [5:0]  crc_skip_cfg;
  reg        crc_init1;
  reg [31:0] crc_poly;
  reg [19:0] pat_mask, pat_val;
  reg        trap_en, trap_pol;
  reg [2:0]  trap_ev;
  reg [5:0]  trap_addr;
  reg [15:0] timeout;

  // ---------------------------------------------------------------- state
  reg [5:0]  pc;
  reg [31:0] x, y, osr, isr;
  reg [5:0]  osr_cnt, isr_cnt;
  reg [3:0]  delay_cnt;
  reg        irq_wait;
  reg        cb_active;
  reg [5:0]  cb_left;
  reg [15:0] div_cnt;
  reg [7:0]  div_acc;
  reg        stuff_on;
  reg [3:0]  run;
  reg        last_bit;
  reg        tx_lvl, rx_lvl, mph;
  reg        stuff_err;
  reg [31:0] crc;
  reg [5:0]  crc_skip;
  reg        ts_prev;
  reg [15:0] last_ts, delta_ts;
  reg        edge_flag;
  reg        trap_armed;

  assign pc_o = pc;

  // ---------------------------------------------------------------- FIFOs
  reg         txf_pop, rxf_push;
  reg  [31:0] rxf_wdata;
  wire [31:0] txf_head;
  wire        txf_empty, txf_full, rxf_full;
  wire [FIFO_AW:0] txl, rxl;
  wire        txf_push = cfg_we && cfg_addr == 4'h4;
  assign txf_level = {{(2-FIFO_AW){1'b0}}, txl};
  assign rxf_level = {{(2-FIFO_AW){1'b0}}, rxl};

  loom_fifo #(.WIDTH(32), .AW(FIFO_AW)) u_txf (
      .clk(clk), .rst_n(rst_n), .clr(fifo_clr),
      .push(txf_push), .wdata(cfg_wdata), .pop(txf_pop),
      .head(txf_head), .full(txf_full), .empty(txf_empty), .level(txl));

  loom_fifo #(.WIDTH(32), .AW(FIFO_AW)) u_rxf (
      .clk(clk), .rst_n(rst_n), .clr(fifo_clr),
      .push(rxf_push), .wdata(rxf_wdata), .pop(rxf_pop),
      .head(rxf_head), .full(rxf_full), .empty(rxf_empty), .level(rxl));

  // ---------------------------------------------------------------- helpers
  function [31:0] rotl(input [31:0] v, input [4:0] s);
    rotl = (v << s) | (v >> (6'd32 - {1'b0, s}));
  endfunction
  function [31:0] nmask(input [5:0] n);  // n in 0..32
    nmask = ~(32'hFFFF_FFFF << n);
  endfunction
  function [31:0] bitrev(input [31:0] v);
    integer i;
    for (i = 0; i < 32; i = i + 1) bitrev[i] = v[31-i];
  endfunction
  function [5:0] sat_add(input [5:0] a, input [5:0] b);
    sat_add = ({1'b0, a} + {1'b0, b} > 7'd32) ? 6'd32 : a + b;
  endfunction

  wire [31:0] pins32  = {12'd0, gpio_in};
  wire [31:0] pins_in = (pins32 >> in_base) | (pins32 << (6'd32 - {1'b0, in_base}));  // rotr
  wire [5:0]  push_th6 = (push_th == 5'd0) ? 6'd32 : {1'b0, push_th};
  wire [5:0]  pull_th6 = (pull_th == 5'd0) ? 6'd32 : {1'b0, pull_th};
  wire [5:0]  crc_w6 = (crc_width == 6'd0 || crc_width > 6'd32) ? 6'd32 : crc_width;
  wire [31:0] crc_wmask = nmask(crc_w6);
  wire [4:0]  crc_top = crc_w6 - 6'd1;
  wire        crc_msb = crc[crc_top];

  // ---------------------------------------------------------------- divider / timestamps
  wire        ts_pin_v = pins32[ts_pin];
  wire        ts_edge_ev = ((ts_pin_v & ~ts_prev) & ts_edge[0]) | ((~ts_pin_v & ts_prev) & ts_edge[1]);
  // In DPLL mode an edge re-phases the divider and suppresses a tick that would coincide with it,
  // so the first sample after any edge lands mid-symbol (otherwise one symbol could be sampled twice).
  wire        tick = en && !restart && (div_cnt == 16'd0) && !(dpll_en && ts_edge_ev);
  wire [15:0] since_edge = ts - last_ts;

  // ---------------------------------------------------------------- events
  wire [7:0] events;
  assign events[0] = ((gpio_in & pat_mask) == (pat_val & pat_mask));
  assign events[1] = stuff_err;
  assign events[2] = (((crc ^ y) & crc_wmask) == 32'd0);   // CRC residue == Y
  assign events[3] = (since_edge > timeout);
  assign events[4] = edge_flag;
  assign events[5] = txf_empty;
  assign events[6] = rxf_full;
  assign events[7] = 1'b1;
  wire trap_fire = trap_armed && (events[trap_ev] == trap_pol);

  // ---------------------------------------------------------------- decode
  wire [2:0] op  = instr[15:13];
  wire [3:0] ds  = instr[12:9];
  wire [8:0] arg = instr[8:0];
  wire [2:0] f_hi = arg[8:6];        // JMP cond / IN src / OUT dst / MOV dst / SET dst
  wire       coded = arg[5];

  wire [2:0] dly_bits = 3'd4 - ss_count;
  wire [3:0] delay    = ds & ~(4'hF << dly_bits);
  wire [3:0] side_raw = ds >> dly_bits;
  wire [2:0] side_n   = ss_count - {2'b0, ss_en};
  wire [3:0] side_v   = side_raw & ~(4'hF << side_n);
  wire       side_act = (ss_count != 3'd0) && (side_n != 3'd0) && (!ss_en || ds[3]);

  wire [5:0] n6 = (arg[4:0] == 5'd0) ? 6'd32 : {1'b0, arg[4:0]};

  // coded ops: n == 0 selects X-counted mode (each data bit decrements X; done when X reaches 0)
  wire        xmode     = (arg[4:0] == 5'd0);
  wire [31:0] x_dec     = x - 32'd1;
  wire [5:0]  cb_cur    = cb_active ? cb_left : n6;
  wire        cnt_last  = xmode ? (x == 32'd1) : (cb_cur == 6'd1);
  wire        cnt_zero  = xmode ? (x == 32'd0) : (cb_cur == 6'd0);
  wire        stuff_now = stuff_on && (c_stuff_n != 4'd0) && (run == c_stuff_n);

  wire exec = tick && !trap_fire && (delay_cnt == 4'd0);

  // ---------------------------------------------------------------- shared datapath
  // OUT: autopull refill happens in the same cycle
  wire        apull_need = autopull && (osr_cnt >= pull_th6);
  wire [31:0] osr_e      = (apull_need && !txf_empty) ? txf_head : osr;
  wire [5:0]  osr_cnt_e  = (apull_need && !txf_empty) ? 6'd0 : osr_cnt;

  // source mux shared by IN and MOV
  reg [31:0] srcv;
  wire [2:0] src_sel = (op == 3'd5) ? arg[2:0] : f_hi;
  always @(*) begin
    case (src_sel)
      3'd0: srcv = pins_in;
      3'd1: srcv = x;
      3'd2: srcv = y;
      3'd3: srcv = 32'd0;
      3'd4: srcv = (op == 3'd5) ? {since_edge, 2'b00, edge_flag, stuff_err, osr_cnt, isr_cnt}
                                : {16'd0, delta_ts};
      3'd5: srcv = crc;
      3'd6: srcv = isr;
      default: srcv = osr;
    endcase
  end

  // funnel: ({f_hi_in, f_lo_in} >> f_amt)[31:0], f_amt in 0..32
  wire        is_in = (op == 3'd2);
  wire [31:0] f_hi_in = is_in ? srcv : 32'd0;
  wire [31:0] f_lo_in = is_in ? isr : osr_e;
  wire [5:0]  f_amt   = (!is_in && !out_shr) ? (6'd32 - n6) : n6;
  wire [63:0] f_full  = {f_hi_in, f_lo_in} >> f_amt;
  wire [31:0] funnel  = f_full[31:0];
  // left shifter: l_in << n6
  wire [31:0] l_in    = is_in ? isr : osr_e;
  wire [31:0] lshift  = l_in << n6;
  wire [31:0] nmask_n = nmask(n6);

  wire [31:0] in_new   = in_shr ? funnel : (lshift | (srcv & nmask_n));
  wire [31:0] out_data = out_shr ? (osr_e & nmask_n) : funnel;
  wire [31:0] out_osr  = out_shr ? funnel : lshift;

  // ---------------------------------------------------------------- next-state logic
  reg [5:0]  nx_pc;
  reg [31:0] nx_x, nx_y, nx_osr, nx_isr;
  reg [5:0]  nx_osr_cnt, nx_isr_cnt;
  reg [3:0]  nx_delay;
  reg        nx_irq_wait, nx_cb_active;
  reg [5:0]  nx_cb_left;
  reg        nx_stuff_on;
  reg [3:0]  nx_run;
  reg        nx_last, nx_tx_lvl, nx_rx_lvl, nx_mph, nx_stuff_err;
  reg [31:0] nx_crc;
  reg        nx_trap_armed, nx_edge_flag;
  // pin write unit request
  reg        pw_en, pw_dir;
  reg [4:0]  pw_base;
  reg [5:0]  pw_cnt;
  reg [31:0] pw_val;
  // scratch
  reg        stall, jump, rpt, cond, wv;
  reg [5:0]  target;
  reg [31:0] mdata;
  reg [5:0]  cnt_sum;
  reg [31:0] new_isr;
  reg        do_push;
  reg        crc_step, crc_bit;
  reg        need_stuff, stuff_bit, lbit, sym, sym_done, dbit, s_in;
  reg [2:0]  irq_idx;
  reg [3:0]  run_inc;
  reg [5:0]  cnt_left;
  reg [5:0]  nx_crc_skip;

  always @(*) begin
    nx_pc        = pc;
    nx_x         = x;
    nx_y         = y;
    nx_osr       = osr;
    nx_isr       = isr;
    nx_osr_cnt   = osr_cnt;
    nx_isr_cnt   = isr_cnt;
    nx_delay     = delay_cnt;
    nx_irq_wait  = irq_wait;
    nx_cb_active = cb_active;
    nx_cb_left   = cb_left;
    nx_stuff_on  = stuff_on;
    nx_run       = run;
    nx_last      = last_bit;
    nx_tx_lvl    = tx_lvl;
    nx_rx_lvl    = rx_lvl;
    nx_mph       = mph;
    nx_stuff_err = stuff_err;
    nx_crc       = crc;
    nx_crc_skip  = crc_skip;
    nx_trap_armed = trap_armed;
    nx_edge_flag = edge_flag | ts_edge_ev;

    pw_en = 1'b0; pw_dir = 1'b0; pw_base = out_base; pw_cnt = out_count; pw_val = 32'd0;
    irq_set = 8'd0; irq_clr = 8'd0;
    txf_pop = 1'b0; rxf_push = 1'b0; rxf_wdata = isr;

    stall = 1'b0; jump = 1'b0; rpt = 1'b0; target = 6'd0; cond = 1'b0; wv = 1'b0;
    mdata = 32'd0; cnt_sum = 6'd0; new_isr = 32'd0; do_push = 1'b0;
    crc_step = 1'b0; crc_bit = 1'b0;
    need_stuff = 1'b0; stuff_bit = 1'b0; lbit = 1'b0; sym = 1'b0; sym_done = 1'b0;
    dbit = 1'b0; s_in = 1'b0; irq_idx = 3'd0; run_inc = (run == 4'hF) ? run : run + 4'd1;

    if (tick && trap_fire) begin
      nx_pc         = trap_addr;
      nx_trap_armed = 1'b0;
      nx_delay      = 4'd0;
      nx_cb_active  = 1'b0;
      nx_irq_wait   = 1'b0;
      nx_mph        = 1'b0;
    end else if (tick && delay_cnt != 4'd0) begin
      nx_delay = delay_cnt - 4'd1;
    end else if (exec) begin
      case (op)
        // ------------------------------------------------ JMP
        3'd0: begin
          case (f_hi)
            3'd0: cond = 1'b1;
            3'd1: cond = (x == 32'd0);
            3'd2: begin cond = (x != 32'd0); nx_x = x - 32'd1; end
            3'd3: cond = (y == 32'd0);
            3'd4: begin cond = (y != 32'd0); nx_y = y - 32'd1; end
            3'd5: cond = (x != y);
            3'd6: cond = pins32[jmp_pin];
            default: cond = (osr_cnt < pull_th6);
          endcase
          if (cond) begin jump = 1'b1; target = arg[5:0]; end
        end
        // ------------------------------------------------ WAIT
        3'd1: begin
          case (arg[7:6])
            2'd0: wv = pins32[arg[4:0]];
            2'd1: wv = pins_in[arg[4:0]];
            2'd2: wv = irq_flags[arg[2:0]];
            default: wv = events[arg[2:0]];
          endcase
          if (wv != arg[8]) stall = 1'b1;
          else if (arg[7:6] == 2'd2 && arg[8]) irq_clr[arg[2:0]] = 1'b1;
        end
        // ------------------------------------------------ IN
        3'd2: begin
          if (coded && (f_hi == 3'd0 || f_hi == 3'd3)) begin
            // coded serial receive (PINS) or decode-and-discard (NULL): one line symbol per execution
            s_in = pins_in[0];
            sym_done = 1'b1;
            case (c_mode)
              2'd1: begin lbit = (s_in != rx_lvl) ? c_nrzi_tog : ~c_nrzi_tog; nx_rx_lvl = s_in; end
              2'd2: begin
                if (!mph) begin sym_done = 1'b0; nx_mph = 1'b1; end
                else begin lbit = s_in ^ c_manch_pol; nx_mph = 1'b0; end
              end
              default: lbit = s_in;
            endcase
            cnt_left = cb_cur;
            nx_cb_left = cnt_left;
            rpt = 1'b1;
            if (sym_done) begin
              if (stuff_now) begin
                // stuffed bit: check and discard
                if (lbit != (c_stuff_mode ? ~last_bit : 1'b0)) nx_stuff_err = 1'b1;
                nx_run  = c_stuff_mode ? 4'd1 : 4'd0;
                nx_last = lbit;
                if (cnt_zero) rpt = 1'b0;
              end else begin
                if (c_stuff_mode) nx_run = (lbit == last_bit) ? run_inc : 4'd1;
                else nx_run = lbit ? run_inc : 4'd0;
                nx_last = lbit;
                if (xmode) nx_x = x_dec;
                else nx_cb_left = cnt_left - 6'd1;
                if (cnt_last && !(stuff_on && c_stuff_n != 4'd0 && nx_run == c_stuff_n)) rpt = 1'b0;
                if (f_hi == 3'd0) begin
                  new_isr  = in_shr ? {lbit, isr[31:1]} : {isr[30:0], lbit};
                  cnt_sum  = sat_add(isr_cnt, 6'd1);
                  crc_step = 1'b1;
                  crc_bit  = lbit;
                  do_push  = 1'b1;
                end
              end
            end
            nx_cb_active = rpt;
          end else begin
            new_isr = in_new;
            cnt_sum = sat_add(isr_cnt, n6);
            do_push = 1'b1;
          end
          if (do_push) begin
            if (autopush && cnt_sum >= push_th6) begin
              if (rxf_full) stall = 1'b1;
              else begin
                rxf_push = 1'b1; rxf_wdata = new_isr;
                nx_isr = 32'd0; nx_isr_cnt = 6'd0;
              end
            end else begin
              nx_isr = new_isr; nx_isr_cnt = cnt_sum;
            end
          end
        end
        // ------------------------------------------------ OUT
        3'd3: begin
          if (apull_need) begin
            if (txf_empty) stall = 1'b1;
            else txf_pop = 1'b1;
          end
          nx_osr = osr_e; nx_osr_cnt = osr_cnt_e;
          if (coded && (f_hi == 3'd0 || f_hi == 3'd3)) begin
            // coded serial transmit (PINS) or CRC feed (NULL): one symbol per execution
            dbit = out_shr ? osr_e[0] : osr_e[31];
            cnt_left = cb_cur;
            nx_cb_left = cnt_left;
            rpt = 1'b1;
            sym_done = 1'b1;
            if (f_hi == 3'd0) begin
              need_stuff = stuff_now;
              stuff_bit  = c_stuff_mode ? ~last_bit : 1'b0;
              lbit       = need_stuff ? stuff_bit : dbit;
              case (c_mode)
                2'd1: begin sym = (lbit == c_nrzi_tog) ? ~tx_lvl : tx_lvl; nx_tx_lvl = sym; end
                2'd2: begin
                  if (!mph) begin sym = c_manch_pol ? lbit : ~lbit; sym_done = 1'b0; nx_mph = 1'b1; end
                  else begin sym = c_manch_pol ? ~lbit : lbit; nx_mph = 1'b0; end
                end
                default: sym = lbit;
              endcase
              pw_en = 1'b1; pw_cnt = c_diff ? 6'd2 : 6'd1; pw_val = {30'd0, ~sym, sym};
            end
            if (sym_done) begin
              if (need_stuff) begin
                nx_run  = c_stuff_mode ? 4'd1 : 4'd0;
                nx_last = stuff_bit;
                if (cnt_zero) rpt = 1'b0;
              end else begin
                nx_osr     = out_shr ? {1'b0, osr_e[31:1]} : {osr_e[30:0], 1'b0};
                nx_osr_cnt = sat_add(osr_cnt_e, 6'd1);
                crc_step   = 1'b1;
                crc_bit    = dbit;
                if (f_hi == 3'd0) begin
                  if (c_stuff_mode) nx_run = (dbit == last_bit) ? run_inc : 4'd1;
                  else nx_run = dbit ? run_inc : 4'd0;
                  nx_last = dbit;
                end
                if (xmode) nx_x = x_dec;
                else nx_cb_left = cnt_left - 6'd1;
                if (cnt_last && !(f_hi == 3'd0 && stuff_on && c_stuff_n != 4'd0 && nx_run == c_stuff_n)) rpt = 1'b0;
              end
            end
            nx_cb_active = rpt;
          end else begin
            nx_osr     = out_osr;
            nx_osr_cnt = sat_add(osr_cnt_e, n6);
            case (f_hi)
              3'd0: begin pw_en = 1'b1; pw_val = out_data; end
              3'd1: nx_x = out_data;
              3'd2: nx_y = out_data;
              3'd4: begin pw_en = 1'b1; pw_dir = 1'b1; pw_val = out_data; end
              3'd5: begin jump = 1'b1; target = out_data[5:0]; end
              3'd6: begin nx_isr = out_data; nx_isr_cnt = n6; end
              default: ;
            endcase
          end
        end
        // ------------------------------------------------ PUSH / PULL
        3'd4: begin
          if (!arg[8]) begin
            if (!(arg[7] && isr_cnt < push_th6)) begin
              if (rxf_full) begin
                if (arg[6]) stall = 1'b1;
                else begin nx_isr = 32'd0; nx_isr_cnt = 6'd0; end
              end else begin
                rxf_push = 1'b1; rxf_wdata = isr;
                nx_isr = 32'd0; nx_isr_cnt = 6'd0;
              end
            end
          end else begin
            if (!(arg[7] && osr_cnt < pull_th6)) begin
              if (txf_empty) begin
                if (arg[6]) stall = 1'b1;
                else begin nx_osr = x; nx_osr_cnt = 6'd0; end
              end else begin
                txf_pop = 1'b1;
                nx_osr = txf_head; nx_osr_cnt = 6'd0;
              end
            end
          end
        end
        // ------------------------------------------------ MOV
        3'd5: begin
          case (arg[5:4])
            2'd0: mdata = srcv;
            2'd1: mdata = ~srcv;
            2'd2: mdata = bitrev(srcv);
            default: mdata = srcv + 32'd1;
          endcase
          case (f_hi)
            3'd0: begin pw_en = 1'b1; pw_val = mdata; end
            3'd1: nx_x = mdata;
            3'd2: nx_y = mdata;
            3'd3: nx_crc = mdata;
            3'd4: begin pw_en = 1'b1; pw_dir = 1'b1; pw_val = mdata; end
            3'd5: begin jump = 1'b1; target = mdata[5:0]; end
            3'd6: begin nx_isr = mdata; nx_isr_cnt = 6'd0; end
            default: begin nx_osr = mdata; nx_osr_cnt = 6'd0; end
          endcase
        end
        // ------------------------------------------------ IRQ
        3'd6: begin
          irq_idx = arg[4] ? {arg[2], arg[1:0] + ID} : arg[2:0];
          if (arg[8]) irq_clr[irq_idx] = 1'b1;
          else if (arg[7]) begin
            if (!irq_wait) begin
              irq_set[irq_idx] = 1'b1; nx_irq_wait = 1'b1; stall = 1'b1;
            end else if (irq_flags[irq_idx]) stall = 1'b1;
            else nx_irq_wait = 1'b0;
          end else irq_set[irq_idx] = 1'b1;
        end
        // ------------------------------------------------ SET
        default: begin
          case (f_hi)
            3'd0: begin pw_en = 1'b1; pw_base = set_base; pw_cnt = {3'b0, set_count}; pw_val = {26'd0, arg[5:0]}; end
            3'd1: nx_x = {26'd0, arg[5:0]};
            3'd2: nx_y = {26'd0, arg[5:0]};
            3'd3: begin
              if (arg[0]) begin nx_crc = crc_init1 ? crc_wmask : 32'd0; nx_crc_skip = crc_skip_cfg; end
              if (arg[1]) begin
                nx_run = 4'd0; nx_last = 1'b0; nx_mph = 1'b0;
                nx_tx_lvl = c_idle; nx_rx_lvl = c_idle;
              end
              if (arg[2]) begin nx_stuff_err = 1'b0; nx_edge_flag = 1'b0; end
              if (arg[3]) nx_trap_armed = trap_en;
              if (arg[5:4] == 2'b01) nx_stuff_on = 1'b0;
              if (arg[5:4] == 2'b10) nx_stuff_on = 1'b1;
              // stuff_pre0: enable stuffing and pre-count one 0 bit (a start bit consumed by WAIT)
              if (arg[5:4] == 2'b11) begin nx_stuff_on = 1'b1; nx_run = 4'd1; nx_last = 1'b0; end
            end
            3'd4: begin pw_en = 1'b1; pw_dir = 1'b1; pw_base = set_base; pw_cnt = {3'b0, set_count}; pw_val = {26'd0, arg[5:0]}; end
            default: ;
          endcase
        end
      endcase

      if (crc_step) begin
        if (crc_skip != 6'd0) nx_crc_skip = crc_skip - 6'd1;
        else nx_crc = ((crc << 1) & crc_wmask) ^ ((crc_bit ^ crc_msb) ? (crc_poly & crc_wmask) : 32'd0);
      end

      if (stall) begin
        // a stalled instruction has no effect except side-set and IRQ-wait bookkeeping
        nx_x = x; nx_y = y; nx_osr = osr; nx_isr = isr;
        nx_osr_cnt = osr_cnt; nx_isr_cnt = isr_cnt; nx_crc = crc; nx_crc_skip = crc_skip;
        nx_cb_active = cb_active; nx_cb_left = cb_left;
        nx_run = run; nx_last = last_bit; nx_tx_lvl = tx_lvl; nx_rx_lvl = rx_lvl; nx_mph = mph;
        nx_stuff_err = stuff_err;
        txf_pop = 1'b0; rxf_push = 1'b0; pw_en = 1'b0;
      end else begin
        nx_delay = delay;
        if (jump) nx_pc = target;
        else if (!rpt) nx_pc = (pc == wrap_top) ? wrap_bot : pc + 6'd1;
      end
    end
  end

  // ---------------------------------------------------------------- pin write unit + side-set
  wire        sd_act = exec && side_act;
  wire [31:0] pw_m   = rotl(nmask(pw_cnt), pw_base);
  wire [31:0] pw_v   = rotl(pw_val, pw_base) & pw_m;
  wire [31:0] sd_m   = rotl({28'd0, ~(4'hF << side_n)}, side_base);
  wire [31:0] sd_v   = rotl({28'd0, side_v}, side_base) & sd_m;
  wire [19:0] i_po_m = (pw_en && !pw_dir) ? pw_m[19:0] : 20'd0;
  wire [19:0] i_pd_m = (pw_en && pw_dir) ? pw_m[19:0] : 20'd0;
  wire [19:0] s_po_m = (sd_act && !ss_pindir) ? sd_m[19:0] : 20'd0;
  wire [19:0] s_pd_m = (sd_act && ss_pindir) ? sd_m[19:0] : 20'd0;
  // side-set has priority over the instruction's own pin writes
  assign po_mask = i_po_m | s_po_m;
  assign po_val  = (pw_v[19:0] & i_po_m & ~s_po_m) | (sd_v[19:0] & s_po_m);
  assign pd_mask = i_pd_m | s_pd_m;
  assign pd_val  = (pw_v[19:0] & i_pd_m & ~s_pd_m) | (sd_v[19:0] & s_pd_m);

  // ---------------------------------------------------------------- registers
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      div_int <= 16'd1; div_frac <= 8'd0;
      wrap_bot <= 6'd0; wrap_top <= 6'd63; start_addr <= 6'd0;
      ss_count <= 3'd0; ss_en <= 1'b0; ss_pindir <= 1'b0; jmp_pin <= 5'd0;
      in_shr <= 1'b0; out_shr <= 1'b0; autopush <= 1'b0; autopull <= 1'b0;
      push_th <= 5'd0; pull_th <= 5'd0;
      out_base <= 5'd0; out_count <= 6'd0; set_base <= 5'd0; set_count <= 3'd0;
      side_base <= 5'd0; in_base <= 5'd0;
      c_mode <= 2'd0; c_nrzi_tog <= 1'b0; c_manch_pol <= 1'b0; c_stuff_en <= 1'b0; c_stuff_mode <= 1'b0;
      c_stuff_n <= 4'd0; c_diff <= 1'b0; c_idle <= 1'b0;
      ts_pin <= 5'd0; ts_edge <= 2'd0; dpll_en <= 1'b0;
      crc_width <= 6'd0; crc_skip_cfg <= 6'd0; crc_init1 <= 1'b0; crc_poly <= 32'd0;
      pat_mask <= 20'd0; pat_val <= 20'd0;
      trap_en <= 1'b0; trap_pol <= 1'b0; trap_ev <= 3'd0; trap_addr <= 6'd0; timeout <= 16'd0;

      pc <= 6'd0; x <= 32'd0; y <= 32'd0; osr <= 32'd0; isr <= 32'd0;
      osr_cnt <= 6'd32; isr_cnt <= 6'd0; delay_cnt <= 4'd0;
      irq_wait <= 1'b0; cb_active <= 1'b0; cb_left <= 6'd0;
      div_cnt <= 16'd0; div_acc <= 8'd0;
      stuff_on <= 1'b0; run <= 4'd0; last_bit <= 1'b0;
      tx_lvl <= 1'b0; rx_lvl <= 1'b0; mph <= 1'b0; stuff_err <= 1'b0;
      crc <= 32'd0; crc_skip <= 6'd0;
      ts_prev <= 1'b0; last_ts <= 16'd0; delta_ts <= 16'd0; edge_flag <= 1'b0;
      trap_armed <= 1'b0;
    end else begin
      // ---- execution state
      pc <= nx_pc; x <= nx_x; y <= nx_y; osr <= nx_osr; isr <= nx_isr;
      osr_cnt <= nx_osr_cnt; isr_cnt <= nx_isr_cnt; delay_cnt <= nx_delay;
      irq_wait <= nx_irq_wait; cb_active <= nx_cb_active; cb_left <= nx_cb_left;
      stuff_on <= nx_stuff_on; run <= nx_run; last_bit <= nx_last;
      tx_lvl <= nx_tx_lvl; rx_lvl <= nx_rx_lvl; mph <= nx_mph; stuff_err <= nx_stuff_err;
      crc <= nx_crc; crc_skip <= nx_crc_skip; trap_armed <= nx_trap_armed; edge_flag <= nx_edge_flag;

      // ---- timestamps (always running)
      ts_prev <= ts_pin_v;
      if (ts_edge_ev) begin
        last_ts  <= ts;
        delta_ts <= ts - last_ts;
      end

      // ---- clock divider
      if (restart || div_sync) begin
        div_cnt <= 16'd0; div_acc <= 8'd0;
      end else if (en) begin
        if (dpll_en && ts_edge_ev) div_cnt <= {1'b0, div_int[15:1]};
        else if (tick) {div_cnt, div_acc} <= {div_int - 16'd1, 8'd0} + {16'd0, div_acc} + {16'd0, div_frac};
        else div_cnt <= div_cnt - 16'd1;
      end

      // ---- restart
      if (restart) begin
        pc <= start_addr; isr_cnt <= 6'd0; osr_cnt <= 6'd32; delay_cnt <= 4'd0;
        irq_wait <= 1'b0; cb_active <= 1'b0; mph <= 1'b0; run <= 4'd0; last_bit <= 1'b0;
        tx_lvl <= c_idle; rx_lvl <= c_idle; stuff_on <= c_stuff_en;
        trap_armed <= trap_en;
      end

      // ---- host config writes
      if (cfg_we) begin
        case (cfg_addr)
          4'h0: begin div_int <= cfg_wdata[31:16]; div_frac <= cfg_wdata[15:8]; end
          4'h1: begin
            wrap_bot <= cfg_wdata[5:0]; wrap_top <= cfg_wdata[11:6]; start_addr <= cfg_wdata[17:12];
            ss_count <= cfg_wdata[20:18]; ss_en <= cfg_wdata[21]; ss_pindir <= cfg_wdata[22];
            jmp_pin <= cfg_wdata[27:23];
          end
          4'h2: begin
            in_shr <= cfg_wdata[0]; out_shr <= cfg_wdata[1]; autopush <= cfg_wdata[2]; autopull <= cfg_wdata[3];
            push_th <= cfg_wdata[8:4]; pull_th <= cfg_wdata[13:9];
          end
          4'h3: begin
            out_base <= cfg_wdata[4:0]; out_count <= cfg_wdata[10:5]; set_base <= cfg_wdata[15:11];
            set_count <= cfg_wdata[18:16]; side_base <= cfg_wdata[23:19]; in_base <= cfg_wdata[28:24];
          end
          4'h6: begin
            c_mode <= cfg_wdata[1:0]; c_nrzi_tog <= cfg_wdata[2]; c_manch_pol <= cfg_wdata[3];
            c_stuff_en <= cfg_wdata[4]; stuff_on <= cfg_wdata[4]; c_stuff_mode <= cfg_wdata[5];
            c_stuff_n <= cfg_wdata[9:6]; c_diff <= cfg_wdata[10]; c_idle <= cfg_wdata[11];
            tx_lvl <= cfg_wdata[11]; rx_lvl <= cfg_wdata[11];
            ts_pin <= cfg_wdata[16:12]; ts_edge <= cfg_wdata[18:17]; dpll_en <= cfg_wdata[19];
          end
          4'h7: begin crc_width <= cfg_wdata[5:0]; crc_init1 <= cfg_wdata[6]; crc_skip_cfg <= cfg_wdata[12:7]; end
          4'h8: crc_poly <= cfg_wdata;
          4'h9: begin crc <= cfg_wdata; crc_skip <= crc_skip_cfg; end
          4'hB: pat_mask <= cfg_wdata[19:0];
          4'hC: pat_val <= cfg_wdata[19:0];
          4'hD: begin
            trap_en <= cfg_wdata[0]; trap_ev <= cfg_wdata[3:1]; trap_pol <= cfg_wdata[4];
            trap_addr <= cfg_wdata[10:5]; timeout <= cfg_wdata[31:16];
            trap_armed <= cfg_wdata[0];
          end
          default: ;
        endcase
      end
    end
  end

`ifdef FORMAL
  // ---------------------------------------------------------------- formal properties (formal/run.sh)
  always @(*) begin
    if (rst_n) begin
      assert (osr_cnt <= 6'd32);
      assert (isr_cnt <= 6'd32);
      // repeat-without-consume: a stuff symbol or a Manchester first half consumes no data
      if (exec && !stall && op == 3'd3 && coded && f_hi == 3'd0 && (need_stuff || !sym_done)) begin
        assert (nx_osr == osr_e);
        assert (nx_osr_cnt == osr_cnt_e);
        assert (nx_x == x);
        assert (nx_crc == crc);
        if (!sym_done) assert (nx_pc == pc);   // a Manchester first half always repeats
      end
      // a discarded (stuffed) receive bit never reaches the ISR, the CRC or the FIFO
      if (exec && !stall && op == 3'd2 && coded && sym_done && stuff_now) begin
        assert (nx_isr == isr);
        assert (nx_isr_cnt == isr_cnt);
        assert (!rxf_push);
        assert (nx_crc == crc);
      end
      // every consumed data bit advances exactly one OSR position
      if (exec && !stall && op == 3'd3 && coded && sym_done && !need_stuff && osr_cnt_e < 6'd32)
        assert (nx_osr_cnt == osr_cnt_e + 6'd1);
      // a stalled instruction has no architectural effect
      if (exec && stall) begin
        assert (nx_x == x && nx_y == y && nx_isr == isr && nx_osr == osr && nx_pc == pc);
        assert (!txf_pop && !rxf_push);
        assert (nx_delay == delay_cnt);
      end
      // FIFO protocol
      if (rxf_push) assert (!rxf_full);
      if (txf_pop) assert (!txf_empty);
      // nothing executes while delayed or not ticking
      if (!tick) assert (nx_pc == pc && !txf_pop && !rxf_push && po_mask == 20'd0 && pd_mask == 20'd0);
    end
  end
`endif

  assign dbg   = {6'd0, osr_cnt, stuff_on, mph, edge_flag, stuff_err, trap_armed, cb_active,
                  irq_wait, 1'b0, isr_cnt, delay_cnt, pc};
  assign dbg_x = x;
endmodule
