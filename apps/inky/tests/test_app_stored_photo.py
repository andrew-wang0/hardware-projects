from __future__ import annotations

from pathlib import Path
from queue import SimpleQueue
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch


for name in (
    "PIL",
    "PIL.Image",
    "gpiozero",
    "gpiozero.pins.lgpio",
    "inky",
    "inky.auto",
    "picamera2",
    "libcamera",
):
    sys.modules.setdefault(name, MagicMock())

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import InkyApp
from hardware import ButtonEvent


class InkyAppStoredPhotoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.display = MagicMock()
        self.controls = MagicMock()
        self.show_light = MagicMock()
        self.events: SimpleQueue[ButtonEvent] = SimpleQueue()
        self.displayed: list[Path] = []
        self.idle: list[int] = []
        self.app = InkyApp(
            camera=MagicMock(),
            display=self.display,
            controls=self.controls,
            signal_led=MagicMock(),
            show_light=self.show_light,
            events=self.events,
            stop_event=threading.Event(),
            image_dir=Path("/tmp"),
            on_photo=MagicMock(),
            on_displayed=self.displayed.append,
            on_display_idle=lambda: self.idle.append(1),
        )
        self.path = Path("/tmp/1700000000.png")

    def test_queues_stored_photo_while_idle(self) -> None:
        self.assertTrue(self.app.queue_stored_photo(self.path))
        self.assertEqual(self.app._take_pending_stored_photo(), self.path)
        self.assertTrue(self.app._busy)
        self.assertFalse(self.app.queue_stored_photo(self.path))

    def test_prepare_capture_cancels_pending_selection(self) -> None:
        self.assertTrue(self.app.queue_stored_photo(self.path))
        self.app._prepare_capture()
        self.assertIsNone(self.app._take_pending_stored_photo())

    def test_shows_stored_photo_without_capturing(self) -> None:
        image = MagicMock()
        prepared = MagicMock()
        opened = MagicMock()
        opened.__enter__.return_value = image
        opened.__exit__.return_value = False
        self.display.prepare.return_value = prepared

        with patch("app.Image.open", return_value=opened):
            self.app._show_stored(self.path)

        self.display.prepare.assert_called_once_with(image)
        self.display.show.assert_called_once_with(prepared)
        self.show_light.start_busy.assert_called_once()
        self.show_light.stop_busy.assert_called_once()
        self.assertEqual(self.displayed, [self.path])
        self.assertFalse(self.app._busy)
        self.controls.set_enabled.assert_any_call(False)
        self.controls.set_enabled.assert_called_with(True)

    def test_failed_show_reverts_home_assistant_state(self) -> None:
        self.display.prepare.side_effect = RuntimeError("panel")
        opened = MagicMock()
        opened.__enter__.return_value = MagicMock()
        opened.__exit__.return_value = False

        with self.assertLogs("app", level="ERROR"):
            with patch("app.Image.open", return_value=opened):
                self.app._show_stored(self.path)

        self.assertEqual(self.idle, [1])
        self.assertEqual(self.displayed, [])
        self.assertFalse(self.app._busy)


if __name__ == "__main__":
    unittest.main()
