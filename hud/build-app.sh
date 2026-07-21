#!/bin/bash
# Build Agent Dash HUD into a double-clickable .app bundle.
#
#   ./build-app.sh            build ./AgentDashHUD.app
#   ./build-app.sh --install  build it and copy it into /Applications
#
# It's a normal menu-bar app: double-click it, or add it to
# System Settings > General > Login Items to start at login. It launches the
# Python snapshot daemon itself. The daemon lives in this repo (main.py + .venv);
# the app finds it either relative to its own location or via the ADDaemonRoot
# path baked in below, so it keeps working even from /Applications as long as
# this repo checkout stays put.
set -euo pipefail

cd "$(dirname "$0")"
APP="AgentDashHUD.app"
CONTENTS="$APP/Contents"
REPO_ROOT="$(cd .. && pwd)"   # holds main.py + .venv (the daemon)

echo "Building release binary..."
swift build -c release

echo "Assembling ${APP}..."
rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"
cp ".build/release/adash-hud" "$CONTENTS/MacOS/adash-hud"
cp "assets/AppIcon.icns" "$CONTENTS/Resources/AppIcon.icns"

# Unquoted heredoc so ${REPO_ROOT} expands into ADDaemonRoot.
cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Agent Dash HUD</string>
    <key>CFBundleDisplayName</key><string>Agent Dash HUD</string>
    <key>CFBundleIdentifier</key><string>com.agentdash.hud</string>
    <key>CFBundleExecutable</key><string>adash-hud</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>CFBundleShortVersionString</key><string>0.1</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <!-- Menu-bar-only: no Dock icon, no app menu. -->
    <key>LSUIElement</key><true/>
    <!-- Where the Python daemon (main.py + .venv) lives, so the app can start
         it even when installed outside the repo (e.g. /Applications). -->
    <key>ADDaemonRoot</key><string>${REPO_ROOT}</string>
</dict>
</plist>
PLIST

if [ "${1:-}" = "--install" ]; then
    echo "Installing to /Applications..."
    rm -rf "/Applications/$APP"
    cp -R "$APP" "/Applications/$APP"
    touch "/Applications/$APP"   # nudge Finder/Dock to pick up the icon
    echo "Installed: /Applications/$APP"
    echo "Open it from Applications or Spotlight (\"Agent Dash HUD\")."
    echo "To start at login: System Settings > General > Login Items > +."
else
    echo "Built: $(pwd)/$APP"
    echo "Install it with:  ./build-app.sh --install   (copies to /Applications)"
fi
