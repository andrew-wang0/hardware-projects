#!/usr/bin/env python3
"""Observe touch without grabbing it; fade with interruptible select waits."""
import math
import os
from pathlib import Path
import select
import signal
import time


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


def main():
    from evdev import InputDevice, ecodes

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
    try:
        write(normal)
        while True:
            now = time.monotonic()
            write(state.level(now))
            readable, _, _ = select.select([touch], [], [], state.wait(now))
            if readable:
                activity = any(event.type in (ecodes.EV_KEY, ecodes.EV_ABS)
                               for event in touch.read())
                if activity:
                    state.touch(time.monotonic())
                    write(normal)
    finally:
        try:
            # Leave the console visible after stopping or a device failure.
            write(normal)
        finally:
            touch.close()


if __name__ == '__main__':
    main()
