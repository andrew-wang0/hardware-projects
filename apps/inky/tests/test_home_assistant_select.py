from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock


for name in (
    "gpiozero",
    "gpiozero.pins.lgpio",
    "inky",
    "inky.auto",
    "picamera2",
    "libcamera",
    "paho",
    "paho.mqtt",
    "paho.mqtt.client",
):
    sys.modules.setdefault(name, MagicMock())

sys.modules["paho.mqtt.client"].MQTT_ERR_SUCCESS = 0
sys.modules["paho.mqtt.client"].CallbackAPIVersion.VERSION2 = 2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config, MqttConfig
from home_assistant import HomeAssistant
from photos import photo_label, photo_object_id, save_displayed

import home_assistant as home_assistant_module

if home_assistant_module.mqtt is not None:
    home_assistant_module.mqtt.MQTT_ERR_SUCCESS = 0


class FakeLight:
    def set(self, **_kwargs) -> None:
        return None

    def state(self) -> tuple[bool, float]:
        return False, 0.0


class FakeMQTTResult:
    rc = 0

    def wait_for_publish(self, timeout: float = 0) -> None:
        return None


class FakeClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, object, int, bool]] = []
        self.subscribed: list[str] = []

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return FakeMQTTResult()

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def username_pw_set(self, *_args, **_kwargs) -> None:
        return None

    def will_set(self, *_args, **_kwargs) -> None:
        return None

    def reconnect_delay_set(self, **_kwargs) -> None:
        return None

    def connect_async(self, *_args, **_kwargs) -> None:
        return None

    def loop_start(self) -> None:
        return None

    def loop_stop(self) -> None:
        return None

    def disconnect(self) -> None:
        return None


def make_config(image_dir: Path, limit: int = 100) -> Config:
    return Config(
        image_dir=image_dir,
        capture_button_pin=24,
        signal_led_pin=12,
        signal_led_active_high=True,
        light_pwm_pin=18,
        light_active_high=True,
        light_brightness=1.0,
        light_pwm_frequency=1_000.0,
        light_minimum_duty=0.20,
        light_transition_seconds=1.0,
        button_bounce_seconds=0.08,
        camera_size=(2304, 1296),
        camera_hflip=True,
        camera_vflip=False,
        camera_exposure_value=0.0,
        camera_ae_settle_seconds=0.5,
        inky_saturation=0.5,
        photo_select_limit=limit,
        mqtt=MqttConfig(
            host="mqtt.local",
            port=1883,
            username="inky",
            password="secret",
            device_id="inky",
            topic_prefix="inky",
            discovery_prefix="homeassistant",
        ),
    )


class HomeAssistantSelectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.image_dir = Path(self._temp.name)
        self.older = self.image_dir / "1700000000.png"
        self.newer = self.image_dir / "1800000000.png"
        self.older.write_bytes(b"old")
        self.newer.write_bytes(b"new")
        os.utime(self.older, (1, 1))
        os.utime(self.newer, (2, 2))
        self.client = FakeClient()
        self.ha = HomeAssistant(make_config(self.image_dir), FakeLight())
        self.ha._client = self.client
        self.queued: list[Path] = []
        self.ha.set_display_handler(self._queue)
        self.ha._on_connect(self.client, None, None, 0, None)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _queue(self, path: Path) -> bool:
        self.queued.append(path)
        return True

    def _payloads(self, topic: str) -> list[object]:
        return [
            payload
            for published, payload, _qos, _retain in self.client.published
            if published == topic
        ]

    def test_discovery_lists_stored_photos_on_the_device(self) -> None:
        configs = self._payloads("homeassistant/select/inky/displayed_photo/config")
        self.assertTrue(configs)
        payload = json.loads(configs[-1])
        self.assertEqual(payload["name"], "Stored Photos")
        self.assertEqual(payload["command_topic"], "inky/photo/select")
        self.assertEqual(
            payload["options"],
            [photo_label(self.newer), photo_label(self.older)],
        )
        self.assertEqual(payload["availability_mode"], "all")
        self.assertEqual(
            payload["availability"][-1],
            {
                "topic": "inky/photo/controls",
                "payload_available": "idle",
                "payload_not_available": "busy",
            },
        )
        self.assertEqual(self._payloads("inky/photo/controls")[-1], "idle")
        self.assertEqual(self._payloads("inky/photo/displayed")[-1], photo_label(self.newer))
        self.assertIn("inky/photo/select", self.client.subscribed)
        self.assertNotIn("inky/photo/previous", self.client.subscribed)
        self.assertNotIn("inky/photo/next", self.client.subscribed)

        current = self._payloads("homeassistant/image/inky/latest_photo/config")
        self.assertTrue(current)
        self.assertEqual(json.loads(current[-1])["name"], "Current Photo")
        self.assertEqual(self._payloads("inky/photo")[-1], b"new")

        self.assertEqual(self._payloads("homeassistant/image/inky/photo_preview/config")[-1], None)
        self.assertEqual(self._payloads("homeassistant/button/inky/previous_photo/config")[-1], None)
        self.assertEqual(self._payloads("homeassistant/button/inky/next_photo/config")[-1], None)
        self.assertEqual(self._payloads("homeassistant/sensor/inky/photo_library/config")[-1], None)
        self.assertEqual(self._payloads("homeassistant/binary_sensor/inky/photo_busy/config")[-1], None)
        newer_id = photo_object_id(self.newer)
        self.assertEqual(
            self._payloads(f"homeassistant/image/inky/{newer_id}/config")[-1],
            None,
        )

    def test_select_command_queues_stored_photo(self) -> None:
        self.client.published.clear()
        self.ha._handle_select_command(photo_label(self.older).encode())

        self.assertEqual(self.queued, [self.older])
        self.assertEqual(self._payloads("inky/photo"), [])
        self.assertEqual(self._payloads("inky/photo/controls")[-1], "busy")
        self.assertEqual(self._payloads("inky/photo/displayed"), [])

    def test_select_command_accepts_filename(self) -> None:
        self.ha._handle_select_command(b"1700000000.png")

        self.assertEqual(self.queued, [self.older])

    def test_busy_controls_ignore_photo_changes(self) -> None:
        self.ha.set_photo_controls_busy()
        self.client.published.clear()
        self.ha._handle_select_command(photo_label(self.older).encode())

        self.assertEqual(self.queued, [])
        self.assertEqual(self._payloads("inky/photo"), [])
        self.assertEqual(self._payloads("inky/photo/displayed")[-1], photo_label(self.newer))

    def test_rejected_queue_does_not_mark_busy(self) -> None:
        self.ha.set_display_handler(lambda _path: False)
        self.client.published.clear()
        self.ha._handle_select_command(photo_label(self.older).encode())

        self.assertEqual(self.queued, [])
        self.assertEqual(self._payloads("inky/photo/controls"), [])
        self.assertEqual(self._payloads("inky/photo/displayed")[-1], photo_label(self.newer))

    def test_invalid_selection_is_ignored(self) -> None:
        self.ha._handle_select_command(b"../secret.png")

        self.assertEqual(self.queued, [])

    def test_current_photo_is_a_noop(self) -> None:
        save_displayed(self.image_dir, self.newer)
        self.ha._displayed = self.newer
        self.ha._handle_select_command(photo_label(self.newer).encode())

        self.assertEqual(self.queued, [])

    def test_showing_a_stored_photo_updates_current_photo(self) -> None:
        self.client.published.clear()
        self.ha.show_stored_photo(self.older)

        self.assertEqual(self._payloads("inky/photo")[-1], b"old")
        self.assertEqual(self._payloads("inky/photo/displayed")[-1], photo_label(self.older))

    def test_reconnect_republishes_the_displayed_photo(self) -> None:
        save_displayed(self.image_dir, self.older)
        self.client.published.clear()
        self.ha._on_connect(self.client, None, None, 0, None)

        self.assertEqual(self._payloads("inky/photo")[-1], b"old")
        self.assertEqual(self._payloads("inky/photo/displayed")[-1], photo_label(self.older))


if __name__ == "__main__":
    unittest.main()
