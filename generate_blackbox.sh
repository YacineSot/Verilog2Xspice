#!/usr/bin/env bash
set -euo pipefail

# Auto-detect standard cell SPICE library (supports Sky130, GF180MCU, IHP)
STDCELL_SPICE=""
for lib_dir in "$PDK_ROOT/$PDK/libs.ref"/*; do
    libname=$(basename "$lib_dir")
    
    if [[ "$libname" == *_sc_* || "$libname" == *stdcell* ]]; then
        # Grab the first .spice file found in the spice/ subdirectory
        spice_file=$(find "$lib_dir/spice" -maxdepth 1 -name "*.spice" -print -quit 2>/dev/null || true)
        if [[ -n "$spice_file" ]]; then
            STDCELL_SPICE="$spice_file"
            break
        fi
    fi
done

if [[ -z "$STDCELL_SPICE" ]]; then
    echo "[ERROR] No standard cell SPICE file found for PDK: $PDK" >&2
    exit 1
fi

echo "[INFO] Using SPICE library: $STDCELL_SPICE"
python3 generate_blackbox.py "$STDCELL_SPICE" "cells_blackbox_${PDK}.v"