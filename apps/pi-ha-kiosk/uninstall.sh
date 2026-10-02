#!/usr/bin/env bash
set -euo pipefail
if (( EUID != 0 )); then
  echo 'Run sudo ./uninstall.sh' >&2
  exit 1
fi
for service in ha-kiosk.service touchscreen-idle.service; do
  if [[ -f "/etc/systemd/system/$service" ]]; then
    systemctl disable --now "$service"
    rm -- "/etc/systemd/system/$service"
  fi
done
rm -f /usr/local/bin/touchscreen-idle.py
systemctl daemon-reload
systemctl enable --now getty@tty1.service
echo 'Removed kiosk services and restored tty1. Packages, groups and Wi-Fi retained.'
