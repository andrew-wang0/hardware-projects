# Inky

`inky` takes still photos with a Raspberry Pi Camera Module and displays them
on an Inky Impression 7.3 Spectra (800×480).

## Behavior

1. Pressing the capture button turns on both signal LEDs and forces the big
   show light to 100% brightness (even if it was off).
2. Releasing the same accepted press captures one photo. Auto-exposure is given
   a short settle time under the capture light so frames are not blown out.
3. Both signal LEDs turn off as soon as capture completes.
4. The big light then breathes smoothly from its current brightness between
   true 0% and 100% PWM every two seconds while the photo is processed, stored,
   published, and shown on the display.
5. The big light fades back to its latest Home Assistant setting (true 0% PWM
   when that setting is off).

Home Assistant can also show a previously taken photo stored on Inky. **Current
Photo** is a sensor with the timestamp of the photo on the panel, or the photo
it is switching to. **Photo** is a dropdown of stored shots (newest first).
Choosing one shows it on the e-paper. That path skips the camera, but still
uses the busy breathing light and ignores the capture button until the refresh
finishes.

**Photo Busy** turns on for the whole capture or e-paper refresh. The light and
photo dropdown become unavailable until that finishes.

Button activity is ignored from the start of capture through the end of the
display refresh. A press that begins during this time remains invalid even if
the button is released after the refresh finishes.

The big show light starts at `LIGHT_BRIGHTNESS` and can be switched or dimmed
through Home Assistant. Capture illumination and the temporary breathing
pattern do not change its Home Assistant state.

The app has no video, preview, countdown, or graphical-desktop dependency.

## Wiring

All GPIO values are BCM numbers.

| Function | BCM | Physical | Notes |
| --- | --- | --- | --- |
| Capture button | GPIO24 | 18 → GND 20 | Also Inky onboard D button |
| External signal LED | GPIO12 | 32 → resistor + LED → GND 34 | Series resistor required |
| Pimoroni shine-through LED | GPIO13 | on Inky board | Mirrors the external signal LED |
| Show-light PWM | GPIO18 | 12 | To MOSFET controller input |
| Show-light signal ground | — | 14 | MOSFET controller ground |

Do not use GPIO16 on physical pin 36 as an output; Inky's onboard C button can
short it to ground.

GPIO12 also reaches the Inky board's EEPROM write-protect input. The app drives
it explicitly and only reads the EEPROM. Never connect an LED directly between
GPIO and ground.

The MOSFET controller must accept 3.3 V PWM logic. Never connect the show-light
power or load directly to a GPIO pin.

## Install

```bash
cd /path/to/pi-projects/apps/inky
./scripts/setup.sh
cp .env.example .env
# Edit .env with the Home Assistant MQTT broker credentials.
chmod 600 .env
sudo reboot
./scripts/install-autostart.sh
```

The installer creates and starts `inky.service`. It also disables and removes
the obsolete `picture.service`.

Useful service commands:

```bash
sudo systemctl restart inky.service
sudo systemctl status inky.service --no-pager --full
sudo journalctl -u inky.service -b --no-pager -n 100
```

## Home Assistant

Install the Mosquitto broker add-on and MQTT integration in Home Assistant,
then create a broker user for Inky. Put its address and credentials in `.env`;
this file is ignored by Git. If `MQTT_HOST` is unset, MQTT is disabled and
local capture continues normally. Capture and display also continue if the
broker is unreachable, credentials are wrong, or the MQTT package is missing.

Inky publishes retained MQTT Discovery configurations. Home Assistant creates
device **Inky** with only:

- `sensor.inky_current_photo`, named **Inky Current Photo**, for the capture
  timestamp on the panel, or the stored photo currently switching onto it.
- `light.inky_light`, named **Inky Light**, for independent on/off and
  brightness control.
- `select.inky_displayed_photo`, named **Inky Photo**, listing stored photos
  newest first by capture timestamp (`YYYY-MM-DD HH:MM:SS`).
- `binary_sensor.inky_photo_busy`, named **Inky Photo Busy**, a diagnostic that
  is on while a capture or e-paper refresh is in progress.

The device publishes online/offline availability. The light and photo dropdown
are also unavailable while **Photo Busy** is on, so Home Assistant greys them
out until the capture or refresh finishes. Current Photo and Photo Busy stay
available so you can see the target shot and that Inky is working.

Light commands fade over one second by default, including ordinary dashboard
toggle and brightness changes. Home Assistant can override that duration per
command. For example, this action fades the big LED to 70% over two seconds:

```yaml
action: light.turn_on
target:
  entity_id: light.inky_light
data:
  brightness_pct: 70
  transition: 2
```

Use `light.turn_off` with the same `transition` field for a smooth fade out.

To show an older photo from an automation, choose its timestamp or stored PNG
filename (`1725892923.png`):

```yaml
action: select.select_option
target:
  entity_id: select.inky_displayed_photo
data:
  option: "2026-09-09 14:22:03"
```

PWM stays at a fixed frequency. Home Assistant off is always true 0% duty, and
on/off commands fade to and from that level. To avoid unstable ultra-short
pulses while the light is on, nonzero brightness is remapped: Home Assistant 1%
uses 20% physical duty, then scales linearly to 100% duty at 100% brightness.
Change `LIGHT_MINIMUM_DUTY` if the hardware needs a different lower bound for
on states only.

`inky/photo/captured` is published only for live camera captures. The payload
is the PNG filename already stored on Inky. Originals remain in Inky's local
`images/` directory. To notify your phone when a new shot is taken:

```yaml
alias: Notify Inky photos
triggers:
  - trigger: mqtt
    topic: inky/photo/captured
actions:
  - action: notify.mobile_app_andrews_iphone
    data:
      title: "New Inky photo"
      message: "{{ trigger.payload }}"
      data:
        entity_id: sensor.inky_current_photo
mode: queued
```

## Image conversion

The camera produces a 2304×1296 RGB image. Picamera2 exposes its `RGB888`
NumPy buffer in BGR byte order, so `camera.py` swaps the red and blue channels
when constructing the Pillow RGB image.

`display.py` then uses `ImageOps.fit` with Lanczos resampling. This
center-crops the camera's 16:9 image to the display's 5:3 aspect ratio and
resizes it to exactly 800×480 without stretching it.

Every fitted image is stored losslessly in `images/` as
`UNIX_TIMESTAMP.png`, with no prefix. Set `INKY_IMAGE_DIR` to use another
directory. The saved PNG is the full-color 800×480 source supplied to Inky
before panel palette conversion.

The fitted RGB image is passed to the official Inky library's
`set_image(..., saturation=0.5)`. Its Spectra driver quantizes and
Floyd–Steinberg dithers the image into the panel's six native colors: black,
white, yellow, red, blue, and green. `show()` sends that buffer to the display
and blocks until the refresh is complete.

## Configuration

- `INKY_IMAGE_DIR=/path/to/images`
- `CAPTURE_BUTTON_PIN=24`
- `SIGNAL_LED_PIN=12`
- `SIGNAL_LED_ACTIVE_HIGH=true`
- `LIGHT_PWM_PIN=18`
- `LIGHT_ACTIVE_HIGH=true`
- `LIGHT_BRIGHTNESS=1.0`
- `LIGHT_PWM_FREQUENCY=1000`
- `LIGHT_MINIMUM_DUTY=0.20`
- `LIGHT_TRANSITION_SECONDS=1.0`
- `BUTTON_BOUNCE_SECONDS=0.08`
- `CAMERA_WIDTH=2304`
- `CAMERA_HEIGHT=1296`
- `CAMERA_HFLIP=true`
- `CAMERA_VFLIP=false`
- `CAMERA_EXPOSURE_VALUE=0.0` (negative darkens if shots are still too bright)
- `CAMERA_AE_SETTLE_SECONDS=0.5` (wait after the capture light turns on)
- `INKY_SATURATION=0.5`
- `INKY_PHOTO_SELECT_LIMIT=100` (newest stored photos listed in Home Assistant)
- `MQTT_HOST=homeassistant.local` (unset disables MQTT)
- `MQTT_PORT=1883`
- `MQTT_USERNAME=inky`
- `MQTT_PASSWORD=...`
- `MQTT_DEVICE_ID=inky`
- `MQTT_TOPIC_PREFIX=inky`
- `MQTT_DISCOVERY_PREFIX=homeassistant`
