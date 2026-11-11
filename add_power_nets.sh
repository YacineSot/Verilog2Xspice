#!/usr/bin/env bash
# add_power_nets.sh
# Injects VDD and VSS pins into SPICE standard cell instance calls.

set -euo pipefail

# Accept file path as argument, fallback to SPICE_WRAPPED env variable
TARGET_FILE="${1:-${SPICE_WRAPPED:-}}"

if [[ ! -f "$TARGET_FILE" ]]; then
    echo "[ERROR] Target SPICE file not found: $TARGET_FILE" >&2
    # Use return if sourced, exit if executed
    return 1 2>/dev/null || exit 1
fi

echo "[INFO] Injecting VDD VSS into standard cell instances in $TARGET_FILE..."

TMP_FILE=$(mktemp)

awk '
/^[[:space:]]*X/ {
    # For lines starting with X (instances), $NF is the last token (cell name).
    # We prepend VDD VSS to the cell name and rebuild the line.
    $NF = "VDD VSS " $NF
    print $0
    next
}
{
    # Print all other lines untouched
    print $0
}
' "$TARGET_FILE" > "$TMP_FILE"

mv "$TMP_FILE" "$TARGET_FILE"
echo "[INFO] Power nets successfully injected."