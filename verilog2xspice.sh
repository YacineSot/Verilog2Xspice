#!/usr/bin/env bash
set -euo pipefail

show_help() {
    cat << 'EOF'
Usage: ./verilog2xspice.sh [OPTIONS] [PROJECT_DIR]

Synthesizes Verilog RTL using Yosys, wraps the gate-level SPICE output with
standard cell power nets, and generates XSPICE digital models and Xschem symbols.

Arguments:
  PROJECT_DIR   Path to the project directory containing RTL files.
                Defaults to the current directory (.).

Options:
  -h, --help, ? Show this help message and exit.

Environment Variables (used by synth.ys):
  PDK_ROOT      Path to the open_pdks root directory (required).
  PDK           Name of the PDK variant (e.g., ihp-sg13g2) (required).
  PDK_NAME      Base name for standard cells (default: sg13cmos5l).
  RTL           Explicit RTL file to synthesize (optional).
  TOP           Top module name (optional, derived from RTL filename otherwise).
EOF
    exit 0
}

# Check for help flags
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || "${1:-}" == "?" ]]; then
    show_help
fi

# Require PROJECT_DIR as the first argument, default to current directory
PROJECT_DIR="${1:-.}"

# Set working directories relative to the project
export RTL_DIR="$PROJECT_DIR"
export OUT_DIR="${PROJECT_DIR}/output/${PDK}"

mkdir -p "$OUT_DIR"

# Generate the shared blackbox in the root directory (where the script is run)
if [[ ! -f cells_blackbox_${PDK}.v ]]; then
    echo "[INFO] Generating blackbox Verilog cells from PDK SPICE..."
    source generate_blackbox.sh
fi

# Export absolute path to the blackbox so Yosys can find it from anywhere
export BLACKBOX_FILE="$PWD/cells_blackbox_${PDK}.v"

YOSYS_VARS_FILE="$OUT_DIR/.yosys_vars"

# Run Yosys (ensure you call synth.tcl if that is the script's actual name)
OUTPUT_TYPE=spice,verilog yosys -c synth.ys


# --- 2. Source the variables Tcl wrote out ---
if [[ ! -f $YOSYS_VARS_FILE ]]; then
    echo "[ERROR] $YOSYS_VARS_FILE not found — did synth.ys succeed?" >&2
    show_help
    exit 1
fi
# shellcheck disable=SC1091
source "$YOSYS_VARS_FILE"

TOP="${YOSYS_TOP}"
LIBS="${YOSYS_LIBERTIES}"
V_NETLIST="$OUT_DIR/${YOSYS_OUT_VERILOG:-${TOP}_synth.v}"
SPICE_RAW="$OUT_DIR/${YOSYS_OUT_SPICE:-${TOP}_synth.spice}"
SPICE_WRAPPED="$OUT_DIR/${TOP}_transistor_level.spice"
XSPICE_OUT="$OUT_DIR/${TOP}"
PDK_REF="${YOSYS_PDK_REF}"
STDCELL_DIR="${YOSYS_STDCELL_DIR}"

echo "[INFO] PROJECT    = $PROJECT_DIR"
echo "[INFO] TOP        = $TOP"
echo "[INFO] Liberty    = $LIBS"
echo "[INFO] Verilog    = $V_NETLIST"
echo "[INFO] SPICE raw  = $SPICE_RAW"

# ---------- 1. Extract port list from Verilog module header ----------
MODULE_PORTS_CSV=$(awk '/^module[[:space:]]+'"$TOP"'[[:space:]]*\(/,/\);/' "$V_NETLIST" | \
    tr '\n' ' ' | sed 's/.*(//; s/).*//' | tr -d ' ')

IFS=',' read -r -a MODULE_PORTS_ARR <<< "$MODULE_PORTS_CSV"

# ---------- 2. Extract direction + width per port ----------
declare -A PORT_DIR
declare -A PORT_WIDTH

while IFS= read -r line; do
    if [[ "$line" =~ ^[[:space:]]*(input|output|inout)[[:space:]]+(\[[0-9]+:[0-9]+\][[:space:]]+)?([a-zA-Z_][a-zA-Z0-9_]*) ]]; then
        dir="${BASH_REMATCH[1]}"
        width_str="${BASH_REMATCH[2]}"
        name="${BASH_REMATCH[3]}"
        PORT_DIR["$name"]="$dir"
        if [[ -n "$width_str" ]]; then
            msb=$(echo "$width_str" | sed 's/\[//; s/:.*//')
            lsb=$(echo "$width_str" | sed 's/.*://; s/\]//')
            PORT_WIDTH["$name"]=$(( msb > lsb ? msb - lsb + 1 : lsb - msb + 1 ))
        else
            PORT_WIDTH["$name"]=1
        fi
    fi
done < "$V_NETLIST"

# ---------- 3. Build .subckt port list ----------
SUBCKT_PORTS=""
for port in "${MODULE_PORTS_ARR[@]}"; do
    w="${PORT_WIDTH[$port]:-1}"
    if (( w == 1 )); then
        SUBCKT_PORTS+="$port "
    else
        for ((i=0; i<w; i++)); do
            SUBCKT_PORTS+="${port}.${i} "
        done
    fi
done
SUBCKT_PORTS="VDD VSS ${SUBCKT_PORTS% }"

# ---------- 4. Build wrapped SPICE file ----------
STD_CELL_SPICE=$(find "$STDCELL_DIR" -type f -name "*.spice" -print -quit)
{
    awk '
        /^[[:space:]]*[XV][0-9]+/ { exit }
        { print }
    ' "$SPICE_RAW"

    echo ".include $STD_CELL_SPICE"
    echo ".subckt ${TOP} ${SUBCKT_PORTS}"

    awk '
        /^[[:space:]]*[XV][0-9]+/ { inst=1 }
        inst {
            if ($0 ~ /^\*+[[:space:]]*$/) exit
            if ($0 ~ /^\*[[:space:]]*end of SPICE netlist/) exit
            print
        }
    ' "$SPICE_RAW"

    echo ".ends ${TOP}"

    awk '
        /^\*+[[:space:]]*$/ { tail=1 }
        tail { print }
    ' "$SPICE_RAW"
} > "$SPICE_WRAPPED"

echo "[INFO] Wrapped SPICE: $SPICE_WRAPPED"
echo "[INFO] Port list: $SUBCKT_PORTS"

# ---------- 5. Call spi2xspice.py ----------
corner_from_lib() {
    local base
    base=$(basename "$1" .lib)

    local proc=""
    if [[ "$base" =~ (^|_)(slow|fast|typical|ss|tt|ff)($|_) ]]; then
        proc="${BASH_REMATCH[2]}"
    fi

    local volt=""
    if [[ "$base" =~ _([0-9]+[pv][0-9]+)V? ]]; then
        volt="${BASH_REMATCH[1]}"
        volt="${volt/p/v}"
    fi

    local temp=""
    if [[ "$base" =~ _([mn]?[0-9]+C)(_|$) ]]; then
        temp="${BASH_REMATCH[1]}"
    fi

    local parts=()
    [[ -n "$proc" ]] && parts+=("$proc")
    [[ -n "$volt" ]] && parts+=("$volt")
    [[ -n "$temp" ]] && parts+=("$temp")

    if [[ ${#parts[@]} -gt 0 ]]; then
        ( IFS=_; echo "${parts[*]}" )
    else
        echo "$base"
    fi
}

for LIB in $LIBS; do
    corner=$(corner_from_lib "$LIB")
    if [ -z "$corner" ] || [ "$corner" = "$(basename "$LIB" .lib)" ]; then
        corner=$(basename "$LIB" .lib)
    fi

    python3 spi2xspice.py "$LIB" \
        -io_time=1p -time=1p -idelay=1p -odelay=1p -cload=2f \
        "$SPICE_WRAPPED" "${XSPICE_OUT}_$corner.lib"
done

echo "[INFO] XSPICE output: $XSPICE_OUT"
echo "[INFO] Generating xschem symbol..."

source gensym3.sh -p "${XSPICE_OUT}_$corner.lib" -o "$OUT_DIR" -s

source add_power_nets.sh "$SPICE_WRAPPED"