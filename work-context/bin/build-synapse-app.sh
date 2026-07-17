#!/usr/bin/env bash
# Build a real, clickable Synapse.app bundle around bin/synapse-app.
#
# The bundle is a thin launcher — it does NOT embed Python. Double-clicking it runs
# this repo's venv against this repo's live data (events.db / config / ~/.secrets),
# exactly like `bin/synapse-app`. The spark icon lives in the bundle, so it shows in
# the Dock and stays there even when the app isn't running.
#
#   bin/build-synapse-app.sh                 # builds dist/Synapse.app
#   bin/build-synapse-app.sh --install       # also installs to ~/Applications
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"     # work-context/
APP="$ROOT/dist/Synapse.app"
C="$APP/Contents"

rm -rf "$APP"
mkdir -p "$C/MacOS" "$C/Resources"

cat > "$C/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Synapse</string>
  <key>CFBundleDisplayName</key><string>Synapse</string>
  <key>CFBundleIdentifier</key><string>app.synapse.desktop</string>
  <key>CFBundleExecutable</key><string>Synapse</string>
  <key>CFBundleIconFile</key><string>synapse</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
PLIST

cat > "$C/MacOS/Synapse" <<LAUNCH
#!/bin/bash
# Launcher — runs the Synapse desktop app from its home repo.
exec "$ROOT/.venv/bin/python" "$ROOT/derive/synapse_app.py" >>"\$HOME/Library/Logs/synapse-app.log" 2>&1
LAUNCH
chmod +x "$C/MacOS/Synapse"

cp "$ROOT/assets/synapse.icns" "$C/Resources/synapse.icns"
printf 'APPL????' > "$C/PkgInfo"

# refresh Launch Services so Finder/Dock pick up the new icon immediately
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" 2>/dev/null || true

echo "built: $APP"

if [[ "${1:-}" == "--install" ]]; then
  DEST="$HOME/Applications/Synapse.app"       # user Applications — no admin needed
  mkdir -p "$HOME/Applications"
  rm -rf "$DEST"
  cp -R "$APP" "$DEST"
  touch "$DEST"
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST" 2>/dev/null || true
  echo "installed: $DEST"
fi
