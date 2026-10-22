#!/usr/bin/env bash
# Build the Rust app and register it for the current user.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID=com.local.UbuntuAppControl
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
BIN="$BIN_DIR/ubuntu-app-control"

# Desktop Entry values use double-quote escaping, not shell quoting.
desktop_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\`/\\\`}"
  value="${value//\$/\\\$}"
  value="${value//%/%%}"
  value="${value//$'\n'/ }"
  printf '"%s"' "$value"
}

cargo build --locked --release --manifest-path "$ROOT/Cargo.toml"
mkdir -p "$BIN_DIR" "$APPS"
install -m 755 "$ROOT/target/release/ubuntu-app-control" "$BIN"

if ! python3 "$ROOT/tools/render_icons.py" "$ROOT/data/$APP_ID.svg" "$ICONS" >/dev/null; then
  echo "warning: could not render icons (install python3-gi, gir1.2-gdkpixbuf-2.0 and librsvg2-common)" >&2
fi

cat > "$APPS/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Ubuntu App Control
GenericName=Application manager
Comment=Manage apps, app data, and GPU launch choices
Exec=$(desktop_quote "$BIN")
Icon=$APP_ID
Terminal=false
Categories=System;GTK;
Keywords=app;apps;uninstall;remove;cache;data;gpu;snap;appimage;
StartupWMClass=$APP_ID
StartupNotify=true
EOF
chmod 644 "$APPS/$APP_ID.desktop"
update-desktop-database "$APPS" 2>/dev/null || true
gtk-update-icon-cache -q -t -f "$ICONS" 2>/dev/null || true

echo "Installed $BIN"
echo "Installed user launcher at $APPS/$APP_ID.desktop"
