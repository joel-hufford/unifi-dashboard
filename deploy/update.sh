#!/usr/bin/env bash
# Pull, sync into /opt, restart. Safe to run repeatedly.
#
# The service runs from /opt/unifi-dashboard, not from your clone - the unit
# sets ProtectHome, so it cannot read /home at all. A git pull on its own
# therefore changes nothing the service sees; this is what carries it across.
set -euo pipefail

APP_DIR=/opt/unifi-dashboard
SERVICE=unifi-dashboard
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -d "$SOURCE_DIR/.git" ]; then
  echo "error: $SOURCE_DIR is not a git clone." >&2
  echo "Run this from your checkout, e.g. ~/unifi-dashboard/deploy/update.sh" >&2
  exit 1
fi

# Re-exec under sudo so the alias can be a bare command.
if [ "$(id -u)" -ne 0 ]; then
  exec sudo -- "$0" "$@"
fi

RUN_USER="${SUDO_USER:-root}"

echo "==> pulling $SOURCE_DIR"
before_req="$(sha256sum "$SOURCE_DIR/requirements.txt" 2>/dev/null | cut -d' ' -f1 || true)"
before_rev="$(sudo -u "$RUN_USER" git -C "$SOURCE_DIR" rev-parse --short HEAD)"
# As the owner, so the pull does not leave root-owned objects in their clone.
sudo -u "$RUN_USER" git -C "$SOURCE_DIR" pull --ff-only
after_rev="$(sudo -u "$RUN_USER" git -C "$SOURCE_DIR" rev-parse --short HEAD)"

if [ "$before_rev" = "$after_rev" ]; then
  echo "    already at $after_rev"
else
  echo "    $before_rev -> $after_rev"
  sudo -u "$RUN_USER" git -C "$SOURCE_DIR" --no-pager log --oneline "$before_rev..$after_rev" | sed 's/^/    /'
fi

echo "==> syncing to $APP_DIR"
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
  --exclude 'config.toml' --exclude 'diagnostics' \
  "$SOURCE_DIR"/ "$APP_DIR"/
chown -R "$RUN_USER" "$APP_DIR"

after_req="$(sha256sum "$SOURCE_DIR/requirements.txt" | cut -d' ' -f1)"
if [ "$before_req" != "$after_req" ]; then
  echo "==> dependencies changed, updating the virtualenv"
  sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
else
  echo "==> dependencies unchanged"
fi

echo "==> restarting $SERVICE"
systemctl restart "$SERVICE"
sleep 2

if systemctl is-active --quiet "$SERVICE"; then
  echo "    running"
  curl -sf --max-time 5 http://127.0.0.1:8787/api/healthz && echo
else
  echo "    FAILED - last log lines:" >&2
  journalctl -u "$SERVICE" -n 20 --no-pager >&2
  exit 1
fi

echo
echo "Refresh the kiosk to pick up front-end changes:  DISPLAY= WAYLAND_DISPLAY=wayland-0 \\"
echo "  or just reboot. The service itself is already on the new code."
