import importlib.util
from pathlib import Path
import unittest
import os
import pwd
import tempfile
from unittest.mock import patch
from types import SimpleNamespace


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


idle = load('idle', 'touchscreen-idle.py')
renderer = load('renderer', 'render-units.py')


class KioskTests(unittest.TestCase):
    def test_evdev_without_context_manager_and_cleanup(self):
        # Distribution evdev InputDevice supports close(), but not __enter__.
        class Device:
            closed = False

            def close(self):
                self.closed = True

        device = Device()
        evdev = SimpleNamespace(InputDevice=lambda path: device,
                                ecodes=SimpleNamespace(EV_KEY=1, EV_ABS=3))
        with tempfile.TemporaryDirectory() as directory:
            backlight = Path(directory)
            (backlight / 'max_brightness').write_text('100')
            (backlight / 'brightness').write_text('0')
            with patch.dict('sys.modules', {'evdev': evdev}), \
                 patch.dict(os.environ, {'BACKLIGHT_DEVICE': directory}, clear=True), \
                 patch.object(idle.signal, 'signal'), \
                 patch.object(idle.Path, 'iterdir', return_value=iter([backlight])), \
                 patch.object(idle.select, 'select', side_effect=SystemExit(0)) as select_mock:
                with self.assertRaises(SystemExit):
                    idle.main()
                select_mock.assert_called_once()
            self.assertTrue(device.closed)
            self.assertEqual((backlight / 'brightness').read_text(), '75')

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
