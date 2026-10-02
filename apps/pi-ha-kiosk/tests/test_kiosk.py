import importlib.util
from pathlib import Path
import unittest
import os
import pwd
import tempfile
from unittest.mock import patch, Mock
from types import SimpleNamespace


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


idle = load('idle', 'touchscreen-idle.py')
renderer = load('renderer', 'render-units.py')


CODES = SimpleNamespace(EV_SYN=0, EV_KEY=1, EV_ABS=3, SYN_REPORT=0,
                        SYN_DROPPED=3, BTN_TOUCH=330, ABS_MT_SLOT=47,
                        ABS_MT_TRACKING_ID=57, ABS_MT_POSITION_X=53)


def event(kind, code, value):
    return SimpleNamespace(type=kind, code=code, value=value)


class KioskTests(unittest.TestCase):
    def test_evdev_without_context_manager_and_cleanup(self):
        # Distribution evdev InputDevice supports close(), but not __enter__.
        class Device:
            closed = False

            def close(self):
                self.closed = True

        device = Device()
        device.grab = Mock()
        device.capabilities = lambda: {1: [330]}
        device.input_props = lambda: [1]
        device.info = SimpleNamespace(vendor=1, product=2, version=1, bustype=3)
        virtual = Mock()
        evdev = SimpleNamespace(InputDevice=lambda path: device,
                                UInput=SimpleNamespace(from_device=Mock(return_value=virtual)),
                                ecodes=CODES)
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
            device.grab.assert_called_once()
            virtual.close.assert_called_once()
            self.assertEqual((backlight / 'brightness').read_text(), '75')

    def test_wake_gesture_suppressed_until_all_fingers_release(self):
        relay = idle.TouchRelay(CODES)
        sync = event(0, 0, 0)
        # Coordinates/slot may arrive before the contact begins.
        relay.feed(event(3, 47, 0), True)
        relay.feed(event(3, 57, 100), False)
        relay.feed(event(1, 330, 1), False)
        self.assertEqual(relay.feed(sync, False), [])
        relay.feed(event(3, 47, 1), False)
        relay.feed(event(3, 57, 101), False)
        self.assertEqual(relay.feed(sync, False), [])
        relay.feed(event(3, 47, 0), False)
        relay.feed(event(3, 57, -1), False)
        self.assertEqual(relay.feed(sync, False), [])
        self.assertTrue(relay.suppress)
        relay.feed(event(3, 47, 1), False)
        relay.feed(event(3, 57, -1), False)
        relay.feed(event(1, 330, 0), False)
        self.assertEqual(relay.feed(sync, False), [])
        self.assertFalse(relay.suppress)
        down = event(1, 330, 1)
        relay.feed(down, False)
        forwarded = relay.feed(sync, False)
        self.assertEqual(forwarded[-2:], [down, sync])
        up = event(1, 330, 0)
        relay.feed(up, False)
        self.assertEqual(relay.feed(sync, False), [up, sync])

    def test_overflow_restarts_instead_of_forwarding_partial_gesture(self):
        with self.assertRaises(RuntimeError):
            idle.TouchRelay(CODES).feed(event(0, 3, 0), False)

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
