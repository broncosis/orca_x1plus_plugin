#!/bin/bash
# deploy_custom_filament.sh
#
# Pushes custom-filament.zip (built by build_custom_filament.py) to an
# X1Plus printer as a manual filament-database override, and reloads
# bbl_screen so it picks it up.
#
# Usage: ./deploy_custom_filament.sh <printer-ip-or-hostname>
#
# Assumes: SSH access as root is already set up (X1Plus default), and
# custom-filament.zip is in the same directory as this script.

set -euo pipefail

PRINTER="${1:?Usage: $0 <printer-ip-or-hostname>}"
LOCAL_ZIP="$(dirname "$0")/custom-filament.zip"
REMOTE_DIR="/userdata/cfg/filament"
REMOTE_ZIP="$REMOTE_DIR/custom-filament.zip"   # no ".sig" in this name -- important

if [ ! -f "$LOCAL_ZIP" ]; then
    echo "custom-filament.zip not found -- run build_custom_filament.py first." >&2
    exit 1
fi

echo "==> Creating $REMOTE_DIR on printer (if needed)"
ssh "root@$PRINTER" "mkdir -p $REMOTE_DIR"

echo "==> Copying $LOCAL_ZIP -> $PRINTER:$REMOTE_ZIP"
scp "$LOCAL_ZIP" "root@$PRINTER:$REMOTE_ZIP"

echo "==> Pointing filament.filename at the override"
ssh "root@$PRINTER" "x1plus settings set filament.filename '$REMOTE_ZIP' --string"

echo "==> Current filament.* settings on the printer:"
ssh "root@$PRINTER" "x1plus settings get 'filament.*'" || true

echo "==> Restarting the screen service so bbl_screen re-reads the resource path"
ssh "root@$PRINTER" "/etc/init.d/S99screen_service restart"

echo
echo "Done. Give the screen ~10-15s to come back up, then check the AMS"
echo "filament picker / Settings > Version > Filament database (should show"
echo "sw_ver = 'Custom') to confirm it picked up the override."
echo
echo "To roll back to the official catalog:"
echo "  ssh root@$PRINTER \"x1plus settings set filament.filename '' --null\""
echo "  ssh root@$PRINTER \"/etc/init.d/S99screen_service restart\""
