// Black-box declaration of the IHP SG13CMOS5L 64x16 single-port SRAM macro for synthesis.
// Simulation uses the PDK behavioural model (test/ihp_sram/, compiled with -DSIM -DFUNCTIONAL).
`ifndef SIM
/* verilator lint_off UNDRIVEN */
(* blackbox *)
module RM_IHPSG13_1P_64x16_c2 (
    input  wire        A_CLK,
    input  wire        A_MEN,
    input  wire        A_WEN,
    input  wire        A_REN,
    input  wire [5:0]  A_ADDR,
    input  wire [15:0] A_DIN,
    input  wire        A_DLY,
    output wire [15:0] A_DOUT
);
endmodule
/* verilator lint_on UNDRIVEN */
`endif
