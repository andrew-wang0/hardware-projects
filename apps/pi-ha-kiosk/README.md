# Raspberry Pi Home Assistant Kiosk

A reproducible Cog/WPE WebKit dashboard client for a Raspberry Pi 3A+,
Raspberry Pi OS Lite **64-bit**, and an 800×480 DSI touchscreen. Cog runs
fullscreen directly on DRM/KMS without a desktop, X11, Chromium or HACS.
Home Assistant runs on a separate LAN server. Its header/sidebar may remain visible.

The backlight starts at 75%, fades to zero after 10 seconds, and wakes immediately
on touch, including during the fade. The first wake touch also reaches Home
Assistant. Input is never grabbed or forwarded through uinput.

## Initial setup

Use Raspberry Pi Imager to flash Raspberry Pi OS Lite 64-bit. Configure hostname,
your own username/password, SSH and optionally Wi-Fi in Imager. Use a suitable
power supply for both Pi and display. Connect via SSH:

```bash
ssh USER@dashboard.local
uname -m # must report aarch64
```

If Wi-Fi is not configured:

```bash
nmcli dev wifi list
sudo nmcli --ask dev wifi connect 'YOUR_SSID'
nmcli device status
ip -4 addr show wlan0
cat /sys/class/net/wlan0/address
```

NetworkManager saves the connection and normally reconnects automatically.
Single quotes also handle SSIDs starting with `!`. Reserve a DHCP IP in the router
against the **Wi-Fi MAC**, rather than an Ethernet MAC. If the profile uses a
randomized MAC, optionally set the permanent MAC (use the actual connection name):

```bash
sudo nmcli connection modify 'YOUR_CONNECTION_NAME' 802-11-wireless.cloned-mac-address permanent
```

## Install after a reflash

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/andrew-wang0/hardware-projects.git
cd hardware-projects/apps/pi-ha-kiosk
cp config.example config
nano config
sudo ./install.sh
sudo reboot
```

Set `KIOSK_USER` to an existing non-root Pi user; `pi` is only an example.
Set `HA_URL` to the LAN address or dedicated dashboard view, for example
`http://homeassistant.local:8123/dashboard-kiosk/home`. Do not embed credentials.
Confirm the stable `TOUCH_DEVICE` symlink using:

```bash
ls -l /dev/input/by-path/
ls /sys/class/backlight/
```

The supplied touchscreen path is from the known Pi 3A+ setup; event numbers may
change, so avoid `/dev/input/event2`. Set `BACKLIGHT_DEVICE` to an absolute sysfs
directory if there are multiple backlights. `SCREEN_TIMEOUT` and `FADE_DURATION`
are positive seconds; `BRIGHTNESS_PERCENT` accepts 1–100.

The installer sources `config` as root: use only your own reviewed shell config.
It validates configuration before installing apt packages, adds the user to
video/render/input, installs two systemd services and disables only tty1 getty.
It enables services without starting them. Re-running it replaces service settings;
reboot afterward to apply them. It does not configure Wi-Fi, Home Assistant, SSH,
boot overlays, or graphics drivers. The OS must already expose DRM/KMS, input and
backlight devices. Package DRM support must be confirmed on the target OS release.

Cog runs as your configured user. systemd creates `/run/ha-kiosk` with mode 0700
and the user's ownership, so no UID 1000 or interactive login is required. It uses
tty1; serial console and SSH remain available. The idle daemon runs as root to
read input and write sysfs brightness. Both services restart after failures.

## Optional Home Assistant trusted login

Create a dedicated **non-admin** Home Assistant user. Reserve the Pi's IP first.
On the Home Assistant server, merge this into the existing configuration (do not
create a duplicate `homeassistant:` section), replacing both placeholders:

```yaml
homeassistant:
  auth_providers:
    - type: trusted_networks
      trusted_networks:
        - KIOSK_IP
      trusted_users:
        KIOSK_IP:
          - KIOSK_USER_ID
      allow_bypass_login: true
    - type: homeassistant
```

Use the user's internal ID, not its display name. Validate the HA configuration
and restart Home Assistant. Trust the kiosk's exact IP, retain the normal auth
provider as fallback, and use a direct LAN URL. A public reverse proxy may cause
HA to see the proxy IP rather than the Pi. IP-based trust lets anyone with that
network identity authenticate; reserve it only on a trusted LAN. Home Assistant
permissions are the security boundary; hiding dashboard navigation is not.
See the [Home Assistant authentication documentation](https://www.home-assistant.io/docs/authentication/providers/#trusted-networks).

## Verify, logs and updates

After reboot:

```bash
systemctl is-active ha-kiosk.service touchscreen-idle.service
systemctl is-enabled getty@tty1.service
systemctl cat ha-kiosk.service touchscreen-idle.service
sudo journalctl -u ha-kiosk.service -n 100 --no-pager
sudo journalctl -u touchscreen-idle.service -n 100 --no-pager
# Follow either service's live logs:
sudo journalctl -u ha-kiosk.service -f
```

Both kiosk services should be active; tty1 getty should be disabled. Verify the
actual dashboard, normal touch, configured brightness, fade after timeout,
instant wake and many repeated sleep/wake cycles. Check SSH and Wi-Fi after
reboot. These hardware checks cannot be replaced by local syntax tests.

To change settings, edit `config`, re-run `sudo ./install.sh` and reboot. To update,
run `git pull --ff-only` in this repository, then reinstall and reboot.

```bash
sudo ./uninstall.sh
```

Uninstall stops and removes both services and the installed Python script,
restores tty1 getty, and retains apt packages, user groups and Wi-Fi configuration.
Cog browser profile data in the user's home is retained.

## Troubleshooting

- **GLES library error:** the installer includes `libgles2`, `libegl1`, `libgbm1`
  and `libgl1-mesa-dri`. Inspect Cog logs for DRM/plugin/permission errors. The
  expected GPU is Broadcom VC4 V3D; do not force software rendering. A desktop or
  another DRM application may compete for the display.
- **Touch missing:** inspect `/dev/input/by-path/`. Optionally use
  `sudo apt install evtest` and `sudo evtest`, then update `TOUCH_DEVICE`.
- **Backlight does not sleep:** inspect the daemon journal and
  `cat /sys/class/backlight/*/{brightness,max_brightness}`. If touch fails,
  stop the observer with `sudo systemctl stop touchscreen-idle.service` to isolate
  the fault. The daemon restores awake brightness when stopped normally.
- **Wi-Fi compatibility:** for an older Pi chipset, try router settings with
  2.4 GHz, WPA2-PSK AES, 20 MHz width, channel 1/6/11. For diagnosis try PMF
  optional, disabling fast roaming/802.11r, MBO/OCE and newer Wi-Fi 6/7 features.
  The installer makes no router changes.

For power and performance:

```bash
vcgencmd get_throttled
vcgencmd measure_clock arm
vcgencmd measure_temp
ps -eo pid,psr,pcpu,pmem,comm,args --sort=-pcpu | head -15
```

Healthy power reports `throttled=0x0`. Around 600 MHz at idle is normal frequency
scaling; under sufficient load a Pi 3A+ can approach 1.4 GHz. Power problems may
appear only with the display connected. One `WPEWebProcess` thread near 100% CPU
can be the Home Assistant frontend's single-thread bottleneck on this 512 MB Pi.
Keep the dashboard light; CPU affinity will not distribute that thread over cores.

## Security and development

Keep passwords, Wi-Fi PSKs, tokens and private keys out of Git. Local `config`,
logs and `.env` files are ignored. No web management interface, browser extension,
HACS integration, automatic updater or additional network listener is installed.
Use distribution packages and do not pipe arbitrary remote scripts into a shell.

Local checks, without installing anything on the development machine:

```bash
bash -n install.sh uninstall.sh
python3 -m py_compile scripts/*.py
python3 -m unittest discover -s tests
# If installed:
shellcheck install.sh uninstall.sh
```
