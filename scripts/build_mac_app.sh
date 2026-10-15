#!/bin/bash
# Builds a double-clickable auto-chope-seat.app that opens the app in its own window.
# Usage: scripts/build_mac_app.sh [destination-folder]   (default: ~/Applications)
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$HOME/Applications}"
APP="$DEST/auto-chope-seat.app"

if [ ! -x "$PROJECT/.venv/bin/python" ]; then
  echo "Missing $PROJECT/.venv. Run the setup steps in README.md first." >&2
  exit 1
fi

mkdir -p "$DEST"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

ICONSET="$APP/Contents/Resources/icon.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$PROJECT/icon.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double_size=$((size * 2))
  sips -z "$double_size" "$double_size" "$PROJECT/icon.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/icon.icns"
rm -r "$ICONSET"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>auto-chope-seat</string>
  <key>CFBundleDisplayName</key><string>auto-chope-seat</string>
  <key>CFBundleIdentifier</key><string>local.auto-chope-seat</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundleExecutable</key><string>auto-chope-seat</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

# The project path is fixed at build time; rebuild the app if the project folder moves.
{
  echo '#!/bin/bash'
  printf 'PROJECT=%q\n' "$PROJECT"
  cat <<'LAUNCHER'
URL="http://127.0.0.1:8766"
LOG="$HOME/Library/Logs/auto-chope-seat.log"

if curl -fsS -m 2 "$URL/api/status" >/dev/null 2>&1; then
  curl -fsS -m 2 -X POST "$URL/api/focus" >/dev/null 2>&1 || open "$URL"
  exit 0
fi

if [ ! -x "$PROJECT/.venv/bin/python" ]; then
  osascript -e "display alert \"auto-chope-seat\" message \"Project not found at $PROJECT. Rebuild the app with scripts/build_mac_app.sh.\" as critical"
  exit 1
fi

cd "$PROJECT" || exit 1
echo "--- $(date) starting auto-chope-seat" >>"$LOG"
nohup "$PROJECT/.venv/bin/python" app.py --window >>"$LOG" 2>&1 &

for _ in $(seq 1 40); do
  sleep 0.5
  curl -fsS -m 1 "$URL/api/status" >/dev/null 2>&1 && exit 0
done
osascript -e "display alert \"auto-chope-seat\" message \"The app did not start. See $LOG for details.\" as critical"
exit 1
LAUNCHER
} > "$APP/Contents/MacOS/auto-chope-seat"
chmod +x "$APP/Contents/MacOS/auto-chope-seat"

echo "Built $APP"
