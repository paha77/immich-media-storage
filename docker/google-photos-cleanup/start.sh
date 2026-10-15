#!/bin/sh
set -eu

mkdir -p "$HOME" /state/firefox-profile
Xvfb "$DISPLAY" -screen 0 1440x1000x24 -nolisten tcp -ac >/tmp/xvfb.log 2>&1 &
xvfb_pid=$!
openbox >/tmp/openbox.log 2>&1 &
openbox_pid=$!
x11vnc -display "$DISPLAY" -forever -shared -nopw -localhost -rfbport 5900 \
  >/tmp/x11vnc.log 2>&1 &
x11vnc_pid=$!
websockify --web=/usr/share/novnc/ 0.0.0.0:6080 127.0.0.1:5900 \
  >/tmp/websockify.log 2>&1 &
websockify_pid=$!

cleanup() {
  kill "$websockify_pid" "$x11vnc_pid" "$openbox_pid" "$xvfb_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

sleep 1
exec python /app/google-photos-cleanup.py "$@"
