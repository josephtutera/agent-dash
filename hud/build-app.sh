#!/bin/bash
# Build Agent Dash HUD into a double-clickable .app bundle.
#
# The result is a normal menu-bar app: double-click it (or add it to System
# Settings > General > Login Items to start at login). It launches the Python
# snapshot daemon itself, so there's nothing else to start. Keep the .app inside
# this worktree — the app locates the daemon (main.py + .venv) by walking up from
# its own location, so moving it elsewhere would break that.
set -euo pipefail

cd "$(dirname "$0")"
APP="AgentDashHUD.app"
CONTENTS="$APP/Contents"

echo "Building release binary…"
swift build -c release

echo "Assembling ${APP}…"
rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS"
cp ".build/release/adash-hud" "$CONTENTS/MacOS/adash-hud"

cat > "$CONTENTS/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Agent Dash HUD</string>
    <key>CFBundleDisplayName</key><string>Agent Dash HUD</string>
    <key>CFBundleIdentifier</key><string>com.agentdash.hud</string>
    <key>CFBundleExecutable</key><string>adash-hud</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>CFBundleShortVersionString</key><string>0.1</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <!-- Menu-bar-only: no Dock icon, no app menu. -->
    <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

echo "Done: $(pwd)/$APP"
echo "Double-click it, or add it to System Settings > General > Login Items to start at login."
