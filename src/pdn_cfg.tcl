# Loom: LibreLane's default pdn_cfg.tcl (Apache-2.0) with the macro grid replaced (see bottom).
# Copyright 2025 LibreLane Contributors
#
# Adapted from OpenLane
#
# Copyright 2020-2022 Efabless Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

source $::env(SCRIPTS_DIR)/openroad/common/io.tcl
source $::env(SCRIPTS_DIR)/openroad/common/set_global_connections.tcl
set_global_connections

set secondary []
foreach vdd $::env(VDD_NETS) gnd $::env(GND_NETS) {
    if { $vdd != $::env(VDD_NET)} {
        lappend secondary $vdd

        set db_net [[ord::get_db_block] findNet $vdd]
        if {$db_net == "NULL"} {
            set net [odb::dbNet_create [ord::get_db_block] $vdd]
            $net setSpecial
            $net setSigType "POWER"
        }
    }

    if { $gnd != $::env(GND_NET)} {
        lappend secondary $gnd

        set db_net [[ord::get_db_block] findNet $gnd]
        if {$db_net == "NULL"} {
            set net [odb::dbNet_create [ord::get_db_block] $gnd]
            $net setSpecial
            $net setSigType "GROUND"
        }
    }
}

set_voltage_domain -name CORE -power $::env(VDD_NET) -ground $::env(GND_NET) \
    -secondary_power $secondary



if { $::env(PDN_MULTILAYER) == 1 } {

    set arg_list [list]
    if { $::env(PDN_ENABLE_PINS) } {
        lappend arg_list -pins "$::env(PDN_VERTICAL_LAYER) $::env(PDN_HORIZONTAL_LAYER)"
    }

    define_pdn_grid \
        -name stdcell_grid \
        -starts_with POWER \
        -voltage_domain CORE \
        {*}$arg_list

    set arg_list [list]
    append_if_equals arg_list PDN_EXTEND_TO "core_ring" -extend_to_core_ring
    append_if_equals arg_list PDN_EXTEND_TO "boundary" -extend_to_boundary

    add_pdn_stripe \
        -grid stdcell_grid \
        -layer $::env(PDN_VERTICAL_LAYER) \
        -width $::env(PDN_VWIDTH) \
        -pitch $::env(PDN_VPITCH) \
        -offset $::env(PDN_VOFFSET) \
        -spacing $::env(PDN_VSPACING) \
        -starts_with POWER \
        {*}$arg_list

    add_pdn_stripe \
        -grid stdcell_grid \
        -layer $::env(PDN_HORIZONTAL_LAYER) \
        -width $::env(PDN_HWIDTH) \
        -pitch $::env(PDN_HPITCH) \
        -offset $::env(PDN_HOFFSET) \
        -spacing $::env(PDN_HSPACING) \
        -starts_with POWER \
        {*}$arg_list

    add_pdn_connect \
        -grid stdcell_grid \
        -layers "$::env(PDN_VERTICAL_LAYER) $::env(PDN_HORIZONTAL_LAYER)"
} else {

    set arg_list [list]
    if { $::env(PDN_ENABLE_PINS) } {
        lappend arg_list -pins "$::env(PDN_VERTICAL_LAYER)"
    }

    define_pdn_grid \
        -name stdcell_grid \
        -starts_with POWER \
        -voltage_domain CORE \
        {*}$arg_list

    set arg_list [list]
    append_if_equals arg_list PDN_EXTEND_TO "core_ring" -extend_to_core_ring
    append_if_equals arg_list PDN_EXTEND_TO "boundary" -extend_to_boundary

    add_pdn_stripe \
        -grid stdcell_grid \
        -layer $::env(PDN_VERTICAL_LAYER) \
        -width $::env(PDN_VWIDTH) \
        -pitch $::env(PDN_VPITCH) \
        -offset $::env(PDN_VOFFSET) \
        -spacing $::env(PDN_VSPACING) \
        -starts_with POWER \
        {*}$arg_list
}

# Adds the standard cell rails if enabled.
if { $::env(PDN_ENABLE_RAILS) == 1 } {
    add_pdn_stripe \
        -grid stdcell_grid \
        -layer $::env(PDN_RAIL_LAYER) \
        -width $::env(PDN_RAIL_WIDTH) \
        -followpins

    add_pdn_connect \
        -grid stdcell_grid \
        -layers "$::env(PDN_RAIL_LAYER) $::env(PDN_VERTICAL_LAYER)"
}


# Adds the core ring if enabled.
if { $::env(PDN_CORE_RING) == 1 } {
    if { $::env(PDN_MULTILAYER) == 1 } {
        set arg_list [list]
        append_if_flag arg_list PDN_CORE_RING_ALLOW_OUT_OF_DIE -allow_out_of_die
        append_if_flag arg_list PDN_CORE_RING_CONNECT_TO_PADS -connect_to_pads
        append_if_equals arg_list PDN_EXTEND_TO "boundary" -extend_to_boundary
        append_if_exists_argument arg_list PDN_CORE_RING_CONNECT_TO_PAD_LAYERS -connect_to_pad_layers

        set pdn_core_vertical_layer $::env(PDN_VERTICAL_LAYER)
        set pdn_core_horizontal_layer $::env(PDN_HORIZONTAL_LAYER)

        if { [info exists ::env(PDN_CORE_VERTICAL_LAYER)] } {
            set pdn_core_vertical_layer $::env(PDN_CORE_VERTICAL_LAYER)
        }

        if { [info exists ::env(PDN_CORE_HORIZONTAL_LAYER)] } {
            set pdn_core_horizontal_layer $::env(PDN_CORE_HORIZONTAL_LAYER)
        }

        add_pdn_ring \
            -grid stdcell_grid \
            -layers "$pdn_core_vertical_layer $pdn_core_horizontal_layer" \
            -widths "$::env(PDN_CORE_RING_VWIDTH) $::env(PDN_CORE_RING_HWIDTH)" \
            -spacings "$::env(PDN_CORE_RING_VSPACING) $::env(PDN_CORE_RING_HSPACING)" \
            -core_offset "$::env(PDN_CORE_RING_VOFFSET) $::env(PDN_CORE_RING_HOFFSET)" \
            {*}$arg_list

        if { [info exists ::env(PDN_CORE_VERTICAL_LAYER)] } {
            add_pdn_connect \
                -grid stdcell_grid \
                -layers "$::env(PDN_CORE_VERTICAL_LAYER) $::env(PDN_HORIZONTAL_LAYER)"
        }

        if { [info exists ::env(PDN_CORE_HORIZONTAL_LAYER)] } {
            add_pdn_connect \
                -grid stdcell_grid \
                -layers "$::env(PDN_CORE_HORIZONTAL_LAYER) $::env(PDN_VERTICAL_LAYER)"
        }

        if { [info exists ::env(PDN_CORE_VERTICAL_LAYER)] && [info exists ::env(PDN_CORE_HORIZONTAL_LAYER)] } {
            add_pdn_connect \
                -grid stdcell_grid \
                -layers "$::env(PDN_CORE_VERTICAL_LAYER) $::env(PDN_CORE_HORIZONTAL_LAYER)"
        }

    } else {
        throw APPLICATION "PDN_CORE_RING cannot be used when PDN_MULTILAYER is set to false."
    }
}

# ---------------------------------------------------------------------------------------------
# Loom: the SRAM macros (RM_IHPSG13_1P_64x16_c2) have their supply pins as Metal4 columns, the same
# layer as the tile's power stripes. pdngen cuts the stripes at every macro and has no same-layer
# way to reach the pins (a macro grid's stripes end 0.48 um short of the core stripes and are then
# removed as floating). The macros are therefore placed so that every core stripe crossing them
# lands on a matching VDD/VSS column (>= 1.9 um from the opposite net), and after pdngen each stripe
# that stops just above an SRAM is extended straight down across it, overlapping the original stripe
# by 1 um. This wraps `pdngen`, which LibreLane's pdn.tcl calls right after sourcing this file.
# ---------------------------------------------------------------------------------------------
proc loom_extend_stripes_over_srams {} {
    set block [ord::get_db_block]
    set m4 [[ord::get_db_tech] findLayer $::env(PDN_VERTICAL_LAYER)]
    set dbu [$block getDefUnits]
    set added 0
    foreach inst [$block getInsts] {
        if { [[$inst getMaster] getName] != "RM_IHPSG13_1P_64x16_c2" } { continue }
        set bb [$inst getBBox]
        set ix0 [$bb xMin]; set ix1 [$bb xMax]; set iy0 [$bb yMin]; set iy1 [$bb yMax]
        foreach netname [list $::env(VDD_NET) $::env(GND_NET)] {
            set net [$block findNet $netname]
            foreach sw [$net getSWires] {
                set boxes [list]
                foreach w [$sw getWires] {
                    if { [$w isVia] || [$w getTechLayer] != $m4 } { continue }
                    set x0 [$w xMin]; set x1 [$w xMax]; set y0 [$w yMin]
                    if { $x1 <= $ix0 || $x0 >= $ix1 } { continue }
                    if { $y0 >= $iy1 && $y0 - $iy1 <= 5 * $dbu } {
                        lappend boxes [list $x0 $iy0 $x1 [expr {$y0 + $dbu}]]
                    }
                }
                foreach b $boxes {
                    odb::dbSBox_create $sw $m4 {*}$b STRIPE
                    incr added
                }
            }
        }
    }
    puts "\[INFO\] Loom: extended $added power stripes across the SRAM macros"
    loom_rebuild_power_pins
}

# Tiny Tapeout's precheck requires every power pin to reach both the top and bottom tile edges.
# pdngen created the pins before the stripes were extended, so rebuild them from the actual metal:
# merge each stripe's segments and declare a pin wherever a stripe spans the full core height.
proc loom_rebuild_power_pins {} {
    set block [ord::get_db_block]
    set m4 [[ord::get_db_tech] findLayer $::env(PDN_VERTICAL_LAYER)]
    set dbu [$block getDefUnits]
    set core [$block getCoreArea]
    set ylo [expr {[$core yMin] + 10 * $dbu}]
    set yhi [expr {[$core yMax] - 10 * $dbu}]
    foreach netname [list $::env(VDD_NET) $::env(GND_NET)] {
        set net [$block findNet $netname]
        set bterm [$block findBTerm $netname]
        if { $bterm == "NULL" } { continue }
        set segs [dict create]
        foreach sw [$net getSWires] {
            foreach w [$sw getWires] {
                if { [$w isVia] || [$w getTechLayer] != $m4 } { continue }
                dict lappend segs "[$w xMin] [$w xMax]" [list [$w yMin] [$w yMax]]
            }
        }
        foreach bp [$bterm getBPins] { odb::dbBPin_destroy $bp }
        set npins 0
        dict for {xs ivs} $segs {
            set ivs [lsort -integer -index 0 $ivs]
            set cur [lindex $ivs 0]
            foreach iv [lrange $ivs 1 end] {
                if { [lindex $iv 0] <= [lindex $cur 1] } {
                    if { [lindex $iv 1] > [lindex $cur 1] } { lset cur 1 [lindex $iv 1] }
                } else {
                    break
                }
            }
            if { [lindex $cur 0] <= $ylo && [lindex $cur 1] >= $yhi } {
                set bp [odb::dbBPin_create $bterm]
                odb::dbBox_create $bp $m4 {*}[lindex $xs 0] [lindex $cur 0] [lindex $xs 1] [lindex $cur 1]
                $bp setPlacementStatus FIRM
                incr npins
            }
        }
        puts "\[INFO\] Loom: $netname has $npins full-height pins"
    }
}

if { [info commands loom_pdngen_orig] == "" } {
    rename pdngen loom_pdngen_orig
    proc pdngen { args } {
        loom_pdngen_orig {*}$args
        loom_extend_stripes_over_srams
    }
}
