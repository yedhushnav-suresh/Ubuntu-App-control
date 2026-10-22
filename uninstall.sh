#!/usr/bin/env bash
# Remove the "Ubuntu App Control" program, launcher and icons.
# App data and the restorable quarantine are left untouched unless --purge is used.
set -euo pipefail

APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
BIN="${XDG_BIN_HOME:-$HOME/.local/bin}/ubuntu-app-control"
APP_ID=com.local.UbuntuAppControl
TARGET="$APPS/$APP_ID.desktop"

if [[ -e "$BIN" ]]; then
  rm -f "$BIN"
  echo "Removed $BIN"
else
  echo "No program installed at $BIN"
fi

if [[ -e "$TARGET" ]]; then
  rm -f "$TARGET"
  echo "Removed $TARGET"
else
  echo "No launcher installed at $TARGET"
fi

# Remove GPU-pinned copies this app created for system launchers. User-owned
# launchers that were edited in place are intentionally left alone.
for override in "$APPS"/uac-*.desktop; do
  [[ -e "$override" ]] || continue
  if grep -q '^X-UAC-GPU=' "$override" 2>/dev/null; then
    rm -f "$override"
    echo "Removed GPU launcher $override"
  fi
done

if [[ -d "$ICONS" ]]; then
  find "$ICONS" -name "$APP_ID.*" -delete 2>/dev/null || true
  echo "Removed the $APP_ID icons from $ICONS"
fi

update-desktop-database "$APPS" 2>/dev/null || true
gtk-update-icon-cache -q -t -f "$ICONS" 2>/dev/null || true

if [[ "${1:-}" == "--purge" ]]; then
  DATA="${XDG_DATA_HOME:-$HOME/.local/share}/ubuntu-app-control"
  CONF="${XDG_CONFIG_HOME:-$HOME/.config}/ubuntu-app-control"
  if [[ -d "$DATA/quarantine" ]] && [[ -n "$(ls -A "$DATA/quarantine" 2>/dev/null)" ]]; then
    echo "Refusing to purge: $DATA/quarantine still holds restorable app data." >&2
    echo "Restore it from the app first, or delete that folder by hand." >&2
    exit 1
  fi
  rm -rf "$DATA" "$CONF"
  echo "Purged $DATA and $CONF"
fi
