#!/usr/bin/env python3
"""Fade the backlight and suppress wake gestures through a persistent input proxy."""
import math
import os
from pathlib import Path
import select
import signal
import socket
import time
from types import SimpleNamespace


def number(name, default, minimum, maximum=float('inf')):
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be between {minimum} and {maximum}')
    return value


class IdleState:
    def __init__(self, timeout, fade_duration, brightness, now):
        self.timeout = timeout
        self.fade_duration = fade_duration
        self.brightness = brightness
        self.last_activity = now

    def touch(self, now):
        self.last_activity = now

    def level(self, now):
        elapsed = now - self.last_activity - self.timeout
        if elapsed <= 0:
            return self.brightness
        return round(self.brightness * max(0, 1 - elapsed / self.fade_duration))

    def wait(self, now):
        elapsed = now - self.last_activity
        if elapsed < self.timeout:
            return self.timeout - elapsed
        if elapsed < self.timeout + self.fade_duration:
            return min(self.fade_duration / 30, self.timeout + self.fade_duration - elapsed)
        return None


class TouchRelay:
    """Filter whole evdev frames and track type-B multitouch until all fingers lift."""
    def __init__(self, codes):
        self.codes = codes
        self.frame = []
        self.slot = 0
        self.contacts = set()
        self.pressed = False
        self.suppress = False
        self.resync = False
        self.absolute = {}
        self.frame_slot = 0
        self.has_slots = False

    @property
    def held(self):
        return self.pressed or bool(self.contacts)

    def feed(self, event, sleeping):
        c = self.codes
        if not self.frame:
            self.frame_slot = self.slot
        if sleeping and event.type in (c.EV_KEY, c.EV_ABS):
            self.suppress = True
        if event.type == c.EV_SYN and event.code == c.SYN_DROPPED:
            # Recreate the virtual device rather than forwarding an incomplete gesture.
            raise RuntimeError('Touch input overflow; restarting input relay')
        if event.type == c.EV_KEY and event.code == c.BTN_TOUCH:
            if event.value and sleeping:
                self.suppress = True
            self.pressed = bool(event.value)
        elif event.type == c.EV_ABS:
            if event.code not in (c.ABS_MT_SLOT, c.ABS_MT_TRACKING_ID):
                # Retain coordinates: evdev may omit unchanged values next time.
                key = (self.slot if event.code >= c.ABS_MT_SLOT else None, event.code)
                self.absolute[key] = event
            if event.code == c.ABS_MT_SLOT:
                self.has_slots = True
                self.slot = event.value
            elif event.code == c.ABS_MT_TRACKING_ID:
                if event.value >= 0:
                    if sleeping:
                        self.suppress = True
                    self.contacts.add(self.slot)
                else:
                    self.contacts.discard(self.slot)
        self.frame.append(event)
        if event.type != c.EV_SYN or event.code != c.SYN_REPORT:
            return []
        result = [] if self.suppress else self.frame
        if result and self.resync:
            prefix = []
            for (slot, code), cached in self.absolute.items():
                if slot is not None:
                    prefix.append(SimpleNamespace(type=c.EV_ABS, code=c.ABS_MT_SLOT, value=slot))
                prefix.append(cached)
            if self.has_slots:
                prefix.append(SimpleNamespace(type=c.EV_ABS, code=c.ABS_MT_SLOT, value=self.frame_slot))
            result = prefix + result
            self.resync = False
        if self.suppress:
            self.resync = True
        self.frame = []
        if not self.held:
            self.suppress = False
        return result


def notify_ready():
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'):
            address = '\0' + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
            connection.sendto(b'READY=1', address)


def main():
    from evdev import InputDevice, UInput, ecodes

    timeout = number('SCREEN_TIMEOUT', '10', 0.001)
    percent = number('BRIGHTNESS_PERCENT', '75', 1, 100)
    fade = number('FADE_DURATION', '1.5', 0.001)
    chosen = os.environ.get('BACKLIGHT_DEVICE', '')
    backlights = sorted(Path('/sys/class/backlight').iterdir())
    if not chosen and len(backlights) != 1:
        raise RuntimeError('Set BACKLIGHT_DEVICE: expected exactly one sysfs backlight')
    backlight = Path(chosen) if chosen else backlights[0]
    maximum = int((backlight / 'max_brightness').read_text().strip())
    if maximum <= 0:
        raise RuntimeError('Backlight max_brightness must be positive')
    normal = max(1, round(maximum * percent / 100))
    brightness_file = backlight / 'brightness'
    state = IdleState(timeout, fade, normal, time.monotonic())
    previous = None

    def write(value):
        nonlocal previous
        if value != previous:
            brightness_file.write_text(str(value))
            previous = value

    def stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    touch = InputDevice(os.environ.get('TOUCH_DEVICE',
                        '/dev/input/by-path/platform-3f205000.i2c-event'))
    virtual = None
    relay = TouchRelay(ecodes)
    try:
        capabilities = touch.capabilities()
        absolute = dict(capabilities.get(ecodes.EV_ABS, []))
        keys = capabilities.get(ecodes.EV_KEY, [])
        if ecodes.BTN_TOUCH not in keys and ecodes.ABS_MT_TRACKING_ID not in absolute:
            raise RuntimeError('Touch device must expose BTN_TOUCH or type-B multitouch tracking')
        if ecodes.ABS_MT_POSITION_X in absolute and ecodes.ABS_MT_SLOT not in absolute:
            raise RuntimeError('Type-A multitouch is unsupported; use a type-B touchscreen')
        # Grab once for the lifetime of the process, never at sleep/wake boundaries.
        touch.grab()
        virtual = UInput.from_device(touch, name='HA Kiosk Touchscreen',
                                    input_props=touch.input_props(),
                                    vendor=touch.info.vendor, product=touch.info.product,
                                    version=touch.info.version, bustype=touch.info.bustype)
        write(normal)
        notify_ready()
        while True:
            now = time.monotonic()
            if relay.held:
                state.touch(now)
            write(state.level(now))
            readable, _, _ = select.select([touch], [], [], state.wait(now))
            if readable:
                for event in touch.read():
                    now = time.monotonic()
                    sleeping = state.level(now) < normal or relay.suppress
                    forwarded = relay.feed(event, sleeping)
                    if event.type in (ecodes.EV_KEY, ecodes.EV_ABS):
                        state.touch(now)
                        write(normal)
                    for output in forwarded:
                        virtual.write_event(output)
    finally:
        try:
            # Leave the console visible after stopping or a device failure.
            write(normal)
        finally:
            try:
                if virtual is not None:
                    virtual.close()
            finally:
                # Closing the physical fd releases its sole grab even on failure.
                touch.close()


if __name__ == '__main__':
    main()
