#!/usr/bin/env bash
set -Eeuo pipefail

Xvfb "$DISPLAY" -screen 0 1280x800x24 -ac -nolisten tcp &
xvfb_pid=$!
openbox --sm-disable &
openbox_pid=$!
x11vnc -display "$DISPLAY" -forever -shared -nopw -rfbport 5900 -listen 127.0.0.1 &
x11vnc_pid=$!
python -m uvicorn lectio_auth_browser.main:app --host 0.0.0.0 --port 8765 &
api_pid=$!
websockify --web /usr/share/novnc/ 0.0.0.0:6080 127.0.0.1:5900 &
websockify_pid=$!

shutdown() {
  kill "$websockify_pid" "$api_pid" "$x11vnc_pid" "$openbox_pid" "$xvfb_pid" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap shutdown EXIT INT TERM

wait -n "$websockify_pid" "$api_pid" "$x11vnc_pid" "$openbox_pid" "$xvfb_pid"
