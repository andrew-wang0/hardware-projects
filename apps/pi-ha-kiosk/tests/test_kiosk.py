import importlib.util
from pathlib import Path
import unittest
import os
import pwd
import tempfile
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


idle = load('idle', 'touchscreen-idle.py')
renderer = load('renderer', 'render-units.py')


class KioskTests(unittest.TestCase):
    def test_fade_and_immediate_wake(self):
        state = idle.IdleState(10, 1.5, 100, 0)
        self.assertEqual(state.level(9), 100)
        self.assertEqual(state.level(10.75), 50)
        self.assertEqual(state.level(12), 0)
        self.assertIsNone(state.wait(12))
        state.touch(12)
        self.assertEqual(state.level(12), 100)
        self.assertEqual(state.wait(12), 10)

    def test_touch_during_fade_restarts_timeout(self):
        state = idle.IdleState(10, 1.5, 100, 0)
        self.assertLessEqual(state.wait(10.5), 0.05)
        state.touch(10.5)
        self.assertEqual(state.level(10.5), 100)
        self.assertEqual(state.level(20.5), 100)
        self.assertEqual(state.level(22), 0)

    def test_render_and_reject_invalid_config(self):
        user = pwd.getpwuid(os.getuid()).pw_name
        if os.getuid() == 0:
            user = 'nobody'
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / 'max_brightness').write_text('255')
            touch = output / 'touch'
            touch.touch()
            config = dict(KIOSK_USER=user, HA_URL='http://ha/view?q=%20&x=$HOME',
                          SCREEN_TIMEOUT='10', BRIGHTNESS_PERCENT='75',
                          FADE_DURATION='1.5', TOUCH_DEVICE=str(touch),
                          BACKLIGHT_DEVICE=directory)
            source = Path(__file__).parents[1] / 'systemd'
            with patch.dict(os.environ, config, clear=True):
                renderer.render(source, output)
                unit = (output / 'ha-kiosk.service').read_text()
                self.assertIn('http://ha/view?q=%%20&x=$$HOME', unit)
                self.assertNotIn('__HA_URL__', unit)
                self.assertIn('RuntimeDirectory=ha-kiosk', unit)
                # A second render must produce the same service.
                renderer.render(source, output)
                self.assertEqual(unit, (output / 'ha-kiosk.service').read_text())
                for key, value in [('SCREEN_TIMEOUT', 'nan'),
                                   ('BRIGHTNESS_PERCENT', '101'),
                                   ('FADE_DURATION', '0'),
                                   ('HA_URL', 'http://user:password@ha'),
                                   ('TOUCH_DEVICE', '/missing/touch')]:
                    with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                        with self.assertRaises(ValueError):
                            renderer.render(source, output)

    def test_systemd_quote(self):
        self.assertEqual(renderer.quote('https://ha/view?q="a"&x=%20'),
                         '"https://ha/view?q=\\"a\\"&x=%%20"')
        with self.assertRaises(ValueError):
            renderer.quote('url\nExecStart=other')


if __name__ == '__main__':
    unittest.main()
