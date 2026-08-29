#!/usr/bin/env bash
# Launch Chromium full-screen against the dashboard. Run from the desktop
# session (labwc/wayfire autostart), not from a system service.
set -euo pipefail

URL="${DASHBOARD_URL:-http://127.0.0.1:8787/}"
PROFILE="${KIOSK_PROFILE:-$HOME/.config/chromium-kiosk}"

# Wait for the dashboard service; on a cold boot Chromium usually wins the race.
for _ in $(seq 1 90); do
  if curl -sfo /dev/null "$URL"; then break; fi
  sleep 1
done

# An unclean shutdown leaves Chromium wanting to show a "restore pages?" bubble
# over the dashboard, where nobody is there to dismiss it.
mkdir -p "$PROFILE/Default"
if [ -f "$PROFILE/Default/Preferences" ]; then
  sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"[^"]*"/"exit_type":"Normal"/' \
    "$PROFILE/Default/Preferences" || true
fi

BROWSER="$(command -v chromium-browser || command -v chromium || true)"
if [ -z "$BROWSER" ]; then
  echo "chromium is not installed: sudo apt install chromium-browser" >&2
  exit 1
fi

FLAGS=(
  --kiosk
  --user-data-dir="$PROFILE"
  # Chromium keeps profile secrets in the system keyring. Under auto-login the
  # login keyring is never unlocked, so it opens an "Authentication Required"
  # dialog at startup and waits - on a wall panel with no keyboard. This
  # profile only ever loads 127.0.0.1 and stores nothing worth protecting.
  --password-store=basic
  --no-first-run
  --no-default-browser-check
  --noerrdialogs
  --disable-infobars
  --disable-session-crashed-bubble
  --disable-features=Translate,TranslateUI
  --disable-pinch
  --overscroll-history-navigation=0
  --check-for-update-interval=31536000
  --autoplay-policy=no-user-gesture-required
)
[ -n "${WAYLAND_DISPLAY:-}" ] && FLAGS+=(--ozone-platform=wayland)

# Hide the pointer when a mouse happens to be plugged in.
command -v unclutter >/dev/null && unclutter -idle 1 &

exec "$BROWSER" "${FLAGS[@]}" "$URL"
