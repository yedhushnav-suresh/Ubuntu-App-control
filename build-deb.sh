#!/usr/bin/env bash
# Build an installable .deb for Ubuntu App Control.
#
#   ./build-deb.sh            -> dist/ubuntu-app-control_<version>_<arch>.deb
#   sudo apt install ./dist/ubuntu-app-control_*.deb
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE=ubuntu-app-control
APP_ID=com.local.UbuntuAppControl
VERSION="${VERSION:-$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$ROOT/Cargo.toml" | head -n1)}"
MAINTAINER="${MAINTAINER:-Yedhushnav Suresh <yedhushnav1@gmail.com>}"
STAGE="$ROOT/build/deb"
DIST="$ROOT/dist"

if [[ -z "$VERSION" ]]; then
  echo "Could not read the package version from Cargo.toml." >&2
  exit 1
fi

rm -rf "$STAGE"
mkdir -p \
  "$STAGE/DEBIAN" \
  "$STAGE/usr/bin" \
  "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/man/man1" \
  "$STAGE/usr/share/doc/$PACKAGE" \
  "$DIST"

# --- native Rust application ----------------------------------------------
cargo build --locked --release --manifest-path "$ROOT/Cargo.toml"
install -m 755 "$ROOT/target/release/$PACKAGE" "$STAGE/usr/bin/$PACKAGE"

# --- desktop entry ---------------------------------------------------------
install -m 644 "$ROOT/packaging/$APP_ID.desktop" \
  "$STAGE/usr/share/applications/$APP_ID.desktop"

# --- icons -----------------------------------------------------------------
python3 "$ROOT/tools/render_icons.py" "$ROOT/data/$APP_ID.svg" \
  "$STAGE/usr/share/icons/hicolor" > /dev/null

# --- documentation ---------------------------------------------------------
install -m 644 "$ROOT/LICENSE" "$STAGE/usr/share/doc/$PACKAGE/copyright"
install -m 644 "$ROOT/packaging/ubuntu-app-control.1" \
  "$STAGE/usr/share/man/man1/$PACKAGE.1"
gzip -9n "$STAGE/usr/share/man/man1/$PACKAGE.1"

# Refresh launchers and icons when the package is installed or upgraded.
cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
EOF
chmod 755 "$STAGE/DEBIAN/postinst"

cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
EOF
chmod 755 "$STAGE/DEBIAN/postrm"

# --- control ---------------------------------------------------------------
INSTALLED_SIZE=$(du -bs --exclude=DEBIAN "$STAGE" | cut -f1 | awk '{print int($1/1024)+1}')
ARCH="$(dpkg --print-architecture)"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PACKAGE
Version: $VERSION
Architecture: $ARCH
Maintainer: $MAINTAINER
Installed-Size: $INSTALLED_SIZE
Depends: libgtk-4-1 (>= 4.10), coreutils, dpkg, pkexec, hicolor-icon-theme
Recommends: snapd, desktop-file-utils
Section: utils
Priority: optional
Description: Review apps, clear their data safely, and choose a GPU
 Lists the applications you installed - Snaps, AppImages and desktop packages -
 rather than the whole package list. For each one it reports how much cache and
 personal data it owns, and offers to clear cache in place, move app data into a
 restorable quarantine, uninstall the app, or pin which graphics card launches
 it by writing a per-user launcher.
EOF

DEB="$DIST/${PACKAGE}_${VERSION}_${ARCH}.deb"
dpkg-deb --build --root-owner-group "$STAGE" "$DEB" > /dev/null

echo "built: $DEB"
dpkg-deb -I "$DEB" | sed -n '2,20p'
echo "--- payload ---"
dpkg-deb -c "$DEB" | awk '{print $6}' | grep -v '/$' | sed "s|$STAGE/||" | sort | head -30
