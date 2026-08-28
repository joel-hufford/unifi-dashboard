#!/usr/bin/env bash
# Install the dashboard on a Raspberry Pi. Idempotent: re-running upgrades the
# code and leaves your config alone.
set -euo pipefail

APP_DIR=/opt/unifi-dashboard
CONFIG_DIR=/etc/unifi-dashboard
SERVICE_USER="${SUDO_USER:-$USER}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "run with sudo: sudo ./deploy/install.sh" >&2
  exit 1
fi

echo "==> installing system packages"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip iputils-ping chromium-browser unclutter

echo "==> copying source to $APP_DIR"
mkdir -p "$APP_DIR"
if [ "$SOURCE_DIR" != "$APP_DIR" ]; then
  rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude 'config.toml' \
    "$SOURCE_DIR"/ "$APP_DIR"/
fi
chown -R "$SERVICE_USER" "$APP_DIR"

echo "==> creating the virtualenv"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  sudo -u "$SERVICE_USER" python3 -m venv "$APP_DIR/.venv"
fi
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> config"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
  install -m 0640 -o root -g "$SERVICE_USER" "$APP_DIR/config.example.toml" "$CONFIG_DIR/config.toml"
  # The service runs with ProtectHome, so the database goes under /var/lib.
  sed -i 's#^db_path = .*#db_path = "/var/lib/unifi-dashboard/history.db"#' "$CONFIG_DIR/config.toml"
  echo "    wrote $CONFIG_DIR/config.toml - edit it before starting the service"
else
  echo "    $CONFIG_DIR/config.toml already exists, left untouched"
fi

echo "==> systemd unit"
sed "s/User=%i/User=$SERVICE_USER/" "$APP_DIR/deploy/unifi-dashboard.service" \
  > /etc/systemd/system/unifi-dashboard.service
systemctl daemon-reload
systemctl enable unifi-dashboard.service

cat <<NEXT

Installed.

  1. Edit $CONFIG_DIR/config.toml (controller address and API key).
  2. sudo systemctl restart unifi-dashboard
     journalctl -u unifi-dashboard -f
  3. Check it on http://127.0.0.1:8787/ then wire up the kiosk browser:
     cp $APP_DIR/deploy/labwc-autostart.example ~/.config/labwc/autostart
  4. Turn off screen blanking: sudo raspi-config -> Display Options.

NEXT
