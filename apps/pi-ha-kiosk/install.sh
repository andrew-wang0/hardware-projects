#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if (( EUID != 0 )); then
  echo 'Run sudo ./install.sh' >&2
  exit 1
fi
if [[ ! -f "$APP_DIR/config" ]]; then
  echo 'Copy config.example to config, edit it, then run sudo ./install.sh.' >&2
  exit 1
fi
if [[ "$(uname -m)" != aarch64 ]]; then
  echo 'This installer targets Raspberry Pi OS Lite 64-bit (aarch64).' >&2
  exit 1
fi
# Config is trusted shell code: review it before executing the installer as root.
set -a
source "$APP_DIR/config"
BACKLIGHT_DEVICE="${BACKLIGHT_DEVICE:-}"
set +a
STAGING_DIR="$(mktemp -d)"
trap 'rm -rf -- "$STAGING_DIR"' EXIT
python3 "$APP_DIR/scripts/render-units.py" "$APP_DIR/systemd" "$STAGING_DIR"
apt-get update
apt-get install -y cog libgles2 libegl1 libgbm1 libgl1-mesa-dri python3 python3-evdev kmod
usermod -aG video,render,input "$KIOSK_USER"
install -o root -g root -m 0755 "$APP_DIR/scripts/touchscreen-idle.py" /usr/local/bin/touchscreen-idle.py
install -o root -g root -m 0644 "$STAGING_DIR/ha-kiosk.service" /etc/systemd/system/ha-kiosk.service
install -o root -g root -m 0644 "$STAGING_DIR/touchscreen-idle.service" /etc/systemd/system/touchscreen-idle.service
systemctl disable --now getty@tty1.service
systemctl daemon-reload
systemctl enable ha-kiosk.service touchscreen-idle.service
cat <<SUMMARY
Installed. Reboot to apply settings and group memberships:
  sudo reboot
Home Assistant: $HA_URL
Kiosk user: $KIOSK_USER (UID $(id -u "$KIOSK_USER"))
Touch device: $TOUCH_DEVICE
Idle timeout: $SCREEN_TIMEOUT seconds
Brightness: $BRIGHTNESS_PERCENT percent
Fade: $FADE_DURATION seconds
SUMMARY
