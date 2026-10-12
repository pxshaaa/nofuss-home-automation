#!/bin/bash
# NoFuss Home — one-command setup on macOS. Safe to run again (it updates in place).
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
LABEL="com.nofuss-home"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

command -v python3 >/dev/null || { echo "Python 3 is needed: install it from python.org or with 'brew install python'."; exit 1; }

echo "→ Installing dependencies…"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
mkdir -p data "$HOME/.config/nofuss-home" "$HOME/Library/Logs"

# Location is only used to calculate sunrise/sunset for automations.
if ! grep -q '"lat": [0-9-]' data/settings.json 2>/dev/null; then
  read -r -p "→ Detect your approximate location for sunrise/sunset automations (uses ipinfo.io)? [Y/n] " yn || yn=n
  if [[ ! "$yn" =~ ^[Nn] ]]; then
    LOC="$(curl -fsS --max-time 5 https://ipinfo.io/loc || true)"
    if [[ "$LOC" == *,* ]]; then
      .venv/bin/python - "$LOC" <<'PY'
import json, sys
from pathlib import Path
p = Path("data/settings.json")
s = json.loads(p.read_text()) if p.exists() else {}
lat, lon = (round(float(x), 2) for x in sys.argv[1].split(","))
s.update(lat=lat, lon=lon)
p.write_text(json.dumps(s, indent=2))
print(f"  saved location {lat}, {lon}")
PY
    else
      echo "  couldn't detect it — you can set \"lat\" and \"lon\" in data/settings.json later."
    fi
  fi
fi

echo "→ Starting NoFuss Home in the background (and at every login)…"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$HERE/.venv/bin/python</string><string>$HERE/app.py</string></array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/nofuss-home.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/nofuss-home.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST"

HOST="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
cat <<MSG

✅ Done. Open on your phone (same Wi-Fi):  http://$HOST.local:8765
   Then Share → "Add to Home Screen" to use it like an app.

First start only: macOS asks "Python would like to use Bluetooth" (and maybe
"…access files on a removable volume") — click Allow on this Mac's screen.

Logs:     tail -f ~/Library/Logs/nofuss-home.log
Spotify:  .venv/bin/python scripts/spotify_login.py <your-client-id>   (see README)
MSG
