#!/bin/bash
# spice2sym_multi.sh
# Processes ALL subcircuits in a SPICE file and generates xschem symbols.
# Usage: ./spice2sym_multi.sh -f GDS_FILE [-o OUTPUT_DIR] [-c CELL] [-e EXTRA_ARGS] [-s]
#    -f GDS_FILE     : GDS file to extract from (required unless -s and SPICE_FILE set)
#    -o OUTPUT_DIR   : output directory (default ./output)
#    -c CELL         : top cell name for kpex
#    -e EXTRA_ARGS   : extra args passed to kpex
#    -s              : symbol-only mode (skip kpex; use existing/provided SPICE_FILE)
#    -p SPICE_FILE   : explicit SPICE file (overrides auto-derived path)
#
# Environment:
#   PDK             : PDK name for kpex (required unless -s)

set -euo pipefail

GDS_FILE=""
OUTPUT_DIR="./output"
CELL=""
EXTRA_ARGS=""
SPICE_FILE=""
STEP=20
SYM_ONLY=false
PDK="${PDK:-}"

usage() {
    cat >&2 <<EOF
Usage: $0 -f GDS_FILE [-o OUTPUT_DIR] [-c CELL] [-e EXTRA_ARGS] [-s] [-p SPICE_FILE]

  -f GDS_FILE     GDS file (required unless -s and -p given)
  -o OUTPUT_DIR   output directory (default: ./output)
  -c CELL         top cell name passed to kpex
  -e EXTRA_ARGS   extra arguments passed to kpex
  -s              symbol-only mode (do not run kpex)
  -p SPICE_FILE   explicit SPICE input file

Environment:
  PDK             PDK name (required unless -s)
EOF
    exit 1
}

while getopts "f:o:c:e:sp:" opt; do
    case ${opt} in
        f) GDS_FILE="$OPTARG" ;;
        o) OUTPUT_DIR="$OPTARG" ;;
        c) CELL="$OPTARG" ;;
        e) EXTRA_ARGS="$OPTARG" ;;
        s) SYM_ONLY=true ;;
        p) SPICE_FILE="$OPTARG" ;;
        *) usage ;;
    esac
done

# Validate required inputs
if [ "$SYM_ONLY" = false ]; then
    [ -n "$GDS_FILE" ] || { echo "Error: -f GDS_FILE is required (or use -s with -p SPICE_FILE)." >&2; usage; }
    [ -n "$PDK" ]      || { echo "Error: PDK environment variable must be set." >&2; exit 1; }
fi

mkdir -p "$OUTPUT_DIR"

# Derive SPICE filename if not provided
if [ -z "$SPICE_FILE" ]; then
    if [ -n "$GDS_FILE" ]; then
        SPICE_FILE="$OUTPUT_DIR/$(basename "${GDS_FILE%.gds}").spice"
    else
        echo "Error: no SPICE file (-p) and no GDS file (-f) to derive it from." >&2
        exit 1
    fi
fi

# Generate SPICE with kpex (unless symbol-only)
if [ "$SYM_ONLY" = false ]; then
    cell_args=()
    if [ -n "$CELL" ]; then
        cell_args=(--cell "$CELL" --magic_rthresh 1)
    fi

    echo "[INFO] Extracting SPICE from $GDS_FILE -> $SPICE_FILE"
    # shellcheck disable=SC2086
    kpex extract --gds "$GDS_FILE" --magic "${cell_args[@]}" \
        --magic_mode RC --out_spice "$SPICE_FILE" $EXTRA_ARGS --pdk "$PDK"
else
    echo "[INFO] Symbol-only mode: using SPICE file $SPICE_FILE"
fi

if [ ! -f "$SPICE_FILE" ]; then
    echo "Error: SPICE file '$SPICE_FILE' not found." >&2
    exit 1
fi

# --- Symbol generation -------------------------------------------------------

# Generate one xschem symbol from a subckt name and its space-separated pins.
generate_symbol() {
    local SYM_NAME="$1"
    local PINS="$2"
    local OUTPUT_FILE="$OUTPUT_DIR/${SYM_NAME}.sym"

    # Pin count (0 if empty)
    local PIN_COUNT=0
    if [ -n "${PINS// /}" ]; then
        PIN_COUNT=$(echo "$PINS" | wc -w)
    fi

    # Geometry
    local HEIGHT=$(( PIN_COUNT * STEP ))
    [ "$HEIGHT" -lt "$STEP" ] && HEIGHT=$STEP
    local CHARS=${#SYM_NAME}
    local WIDTH=$(( CHARS * 10 + 30 ))

    # Pin half-height for boxes (float-safe: use integers)
    local PIN_HALF=3

    {
        # Header
        cat <<EOF
v {xschem version=3.4.BRC file_version=1.4}
G {}
K{type=primitive
format="@name @pinlist @symname"
template="name=X1"
}
EOF

        # Pins on left side: line + text label + rectangle
        local i=0
        local pin
        for pin in $PINS; do
            local Y=$(( 10 + i * STEP ))
            local Y1=$(( Y - PIN_HALF ))
            local Y2=$(( Y + PIN_HALF ))
            # Pin stub (left of body)
            printf 'L 4 -20 %d 0 %d {}\n' "$Y" "$Y"
            # Pin name label near the stub
            printf 'T {%s} 5 %d 0 0 0.2 0.2 {}\n' "$pin" "$(( Y - 5 ))"
            # Pin connection box
            printf 'B 5 -22.5 %d -17.5 %d {name=%s dir=inout}\n' "$Y1" "$Y2" "$pin"
            i=$(( i + 1 ))
        done

        # Body rectangle: P 4 x1 y1 x2 y2 x3 y3 x4 y4
        # Rectangle from (0,0) to (WIDTH, HEIGHT)
        printf 'P 4 5 %d 0 0 0 0 %d %d %d %d 0 {}\n' "$WIDTH" "$HEIGHT" "$WIDTH" "$HEIGHT" "$WIDTH"

        # Symbol name (centered-ish) and instance name
        printf 'T {@symname} 40 %d 0 0 0.3 0.3 {}\n' "$(( HEIGHT / 2 - 10 ))"
        printf 'T {@name} 0 -15 0 0 0.2 0.2 {}\n'
    } > "$OUTPUT_FILE"

    echo "Generated $OUTPUT_FILE ($PIN_COUNT pins)"
}

# --- Parse SPICE and generate symbols ---------------------------------------

echo "Processing $SPICE_FILE..."

JOINED_FILE=$(mktemp)
trap 'rm -f "$JOINED_FILE"' EXIT

# Join SPICE continuation lines (lines starting with '+') into logical lines,
# one logical line per output line (no spurious blank lines).
awk '
  /^[[:space:]]*\+/ {
    sub(/^[[:space:]]*\+[[:space:]]*/, " ")
    printf "%s", $0
    next
  }
  {
    if (NR > 1) printf "\n"
    printf "%s", $0
  }
  END { printf "\n" }
' "$SPICE_FILE" > "$JOINED_FILE"

current_subckt=""
current_pins=""
in_subckt=false

while IFS= read -r line || [ -n "$line" ]; do
    # Strip trailing CR (Windows line endings) and comments after first '*'
    line="${line%$'\r'}"

    if [[ "$line" =~ ^[[:space:]]*\.subckt[[:space:]]+([^[:space:]]+)(.*)$ ]]; then
        # Save previous subckt if any (defensive)
        if [ -n "$current_subckt" ]; then
            generate_symbol "$current_subckt" "$current_pins"
        fi
        current_subckt="${BASH_REMATCH[1]}"
        # Remaining text after subckt name = pins (tokens); filter out
        # any params=... tokens (SPICE allows .subckt name pin1 pin2 PARAM=val)
        local_pins="${BASH_REMATCH[2]}"
        # shellcheck disable=SC2086
        current_pins=$(echo "$local_pins" | tr ' ' '\n' \
            | grep -Ev '^$|=' | paste -sd' ' -)
        in_subckt=true

    elif [[ "$line" =~ ^[[:space:]]*\.ends ]] && [ "$in_subckt" = true ]; then
        if [ -n "$current_subckt" ]; then
            generate_symbol "$current_subckt" "$current_pins"
        fi
        current_subckt=""
        current_pins=""
        in_subckt=false
    fi
    # NOTE: no "continuation pin" branch — continuation lines were already
    # joined by awk above.
done < "$JOINED_FILE"

# Handle a subckt that never got a .ends
if [ -n "$current_subckt" ]; then
    generate_symbol "$current_subckt" "$current_pins"
fi

echo "Done. Symbols saved in $OUTPUT_DIR/"