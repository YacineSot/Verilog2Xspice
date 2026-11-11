# Verilog-to-XSPICE Mixed-Signal Simulation Flow

A robust, fully automated toolchain for converting Verilog RTL into accurately timed, gate-level XSPICE netlists for mixed-signal simulation in ngspice.

This flow bridges the gap between digital synthesis (Yosys) and analog simulation (ngspice) by replacing transistor-level standard cell definitions with fast, event-driven XSPICE models (`d_lut`, `d_dff`, `d_dlatch`). It supports Open PDKs (IHP SG13G2, SkyWater Sky130, GlobalFoundries GF180MCU) out-of-the-box.

## 🚀 Key Features & Updates

This toolchain builds upon [Tim Edwards's Qflow](https://github.com/RTimothyEdwards/qflow.git) original `spi2xspice.py`, introducing massive structural upgrades to achieve STA-level timing accuracy and robust mixed-signal integration:

* **Topological Slew Propagation:** Abandons static input slew assumptions. The script builds a directed circuit graph, recursively propagates transition times from driving gates to driven gates, and handles combinational loop-breaking (e.g., ring oscillators) automatically.
* **True 2D Bilinear Interpolation:** Replaces monolithic median plane-fitting. Delays and transition times are dynamically interpolated directly from Liberty (`.lib`) 2D lookup tables based on the exact arriving slew and extracted net capacitive load.
* **Automated SPICE Port Alignment:** Eliminates the infamous "scrambled SPICE ports" issue in Yosys. An auto-generated Verilog blackbox forces Yosys to output gate-level SPICE that perfectly aligns with the PDK's internal `.subckt` pin sequencing.

---

## 🛠 Prerequisites

Ensure the following tools are installed and available in your system `$PATH`:

* **Yosys** (Synthesis)
* **Python 3.x** (Netlist and Liberty parsing)
* **Ngspice** (Simulation, compiled with XSPICE support)
* **Xschem** (For symbol generation)
* **open_pdks** (Standard cell libraries and Liberty files)

## 📁 Repository Manifest

| File | Description |
| --- | --- |
| `verilog2xspice.sh` | The main execution wrapper. Manages environments, runs Yosys, triggers post-processing, and generates Xschem symbols. |
| `synth.ys` | Yosys Tcl script. Auto-discovers RTL, loads the port-alignment blackbox, maps to the lowest-voltage typical Liberty corner, and outputs gate-level SPICE/Verilog. |
| `spi2xspice.py` | The core Python conversion engine. Parses Liberty timing, extracts topological net loads, calculates exact delays, and injects dynamic XSPICE `.model` definitions. |
| `generate_blackbox.sh/py` | Auto-discovers the active PDK's standard cell `.spice` library and generates a `cells_blackbox_<PDK>.v` wrapper to strictly enforce SPICE positional port mapping in Yosys. |
| `add_power_nets.sh` | A fast `awk` script that injects `VDD` and `VSS` nodes into the Yosys-generated SPICE instance lines for transistor level simulations. |
| `gensym3.sh` | Auto-generates Xschem `.sym` files for the final XSPICE macros so they can be immediately dropped into analog testbenches. |

---

## ⚙️ Usage & Workflow

### 1. Environment Setup

The tool relies on standard open-source EDA environment variables. At minimum, you must define your PDK paths:

```bash
export PDK_ROOT="/path/to/share/pdk"
export PDK="sg13g2" # or sky130A, gf180mcuC, etc.

```

### 2. Execution

Run the main wrapper, optionally passing your project directory containing the Verilog RTL. If no directory is provided, it defaults to the current working directory.

```bash
# Print help menu
./verilog2xspice.sh --help

# Run on the current directory
./verilog2xspice.sh

# Run on a specific project directory
./verilog2xspice.sh /path/to/my_verilog_project

```

### 3. What Happens Under the Hood?

1. **Blackbox Generation:** `generate_blackbox.sh` scans `$PDK_ROOT`, finds the standard cell SPICE library, and generates `cells_blackbox_<PDK>.v` in the root folder.
2. **Synthesis:** Yosys (`synth.ys`) reads your RTL, loads the blackbox to lock port ordering, maps logic using the PDK's `.lib`, and writes a raw SPICE netlist to `output/<TOP>_synth.spice`.
3. **Power Injection:** `add_power_nets.sh` intercepts the raw SPICE netlist and injects `VDD VSS` into every gate instance call.
4. **Timing & Conversion:** `spi2xspice.py` parses the wrapped SPICE and the Liberty file. It evaluates the topological capacitance on every net, recursively calculates incoming slew rates, computes unique delays for every single gate, and writes the final XSPICE library.
5. **Symbol Generation:** `gensym3.sh` creates an Xschem symbol for your newly synthesized digital block.

---
