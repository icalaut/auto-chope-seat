#!/bin/sh
# Starts the auto-chope-seat phone-control server on this Mac.
# Open the printed URL on your iPhone (same Wi-Fi), enter the token once,
# then use Safari's Share -> "Add to Home Screen" for a full-screen app.
set -e
cd "$(dirname "$0")/.."
TOKEN_FILE=".acs_token"
if [ ! -s "$TOKEN_FILE" ]; then
  openssl rand -hex 12 > "$TOKEN_FILE"
fi
TOKEN=$(cat "$TOKEN_FILE")
IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "<this-mac-ip>")
echo "iPhone URL:    http://$IP:8000"
echo "Access token:  $TOKEN"
exec env ACS_TOKEN="$TOKEN" .venv/bin/python server.py
