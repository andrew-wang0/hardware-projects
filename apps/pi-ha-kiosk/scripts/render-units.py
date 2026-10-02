#!/usr/bin/env python3
"""Validate exported config and render systemd values without expansion."""
import math
import os
from pathlib import Path
import pwd
import sys
from urllib.parse import urlsplit


def quote(value):
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('Configuration cannot contain control characters')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def render(source, output):
    user = os.environ.get('KIOSK_USER', '')
    account = pwd.getpwnam(user)
    if account.pw_uid == 0:
        raise ValueError('KIOSK_USER must be a non-root existing user')
    url = os.environ.get('HA_URL', '')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('HA_URL must be an HTTP(S) URL without credentials')
    values = {name: os.environ.get(name, '') for name in (
        'SCREEN_TIMEOUT', 'BRIGHTNESS_PERCENT', 'FADE_DURATION',
        'TOUCH_DEVICE', 'BACKLIGHT_DEVICE')}
    for name in ('SCREEN_TIMEOUT', 'BRIGHTNESS_PERCENT', 'FADE_DURATION'):
        value = float(values[name])
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f'{name} must be a positive finite number')
        if name == 'BRIGHTNESS_PERCENT' and not 1 <= value <= 100:
            raise ValueError('BRIGHTNESS_PERCENT must be between 1 and 100')
    if not Path(values['TOUCH_DEVICE']).is_absolute() or not Path(values['TOUCH_DEVICE']).exists():
        raise ValueError('TOUCH_DEVICE must be an existing absolute path; inspect ls -l /dev/input/by-path/')
    chosen = values['BACKLIGHT_DEVICE']
    if chosen and (not Path(chosen).is_absolute() or not (Path(chosen) / 'max_brightness').is_file()):
        raise ValueError('BACKLIGHT_DEVICE must be an absolute backlight directory')
    if not chosen and len(list(Path('/sys/class/backlight').glob('*'))) != 1:
        raise ValueError('Expected one backlight; set BACKLIGHT_DEVICE explicitly')
    quote(user)  # Reject control characters even for the unquoted User directive.
    replacements = {'KIOSK_USER': user, 'HA_URL': quote(url).replace('$', '$$')}
    replacements.update({key: quote(f'{key}={value}') for key, value in values.items()})
    for template, target in (('ha-kiosk.service.template', 'ha-kiosk.service'),
                             ('touchscreen-idle.service', 'touchscreen-idle.service')):
        text = (source / template).read_text()
        # One pass prevents user values being interpreted as template markers.
        import re
        text = re.sub(r'__([A-Z_]+)__', lambda match: replacements[match[1]], text)
        (output / target).write_text(text)


if __name__ == '__main__':
    try:
        render(Path(sys.argv[1]), Path(sys.argv[2]))
    except (ValueError, KeyError, OSError) as error:
        sys.exit(f'Configuration error: {error}')
