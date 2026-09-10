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
    def __init__(self) -> None:
        self.commands: list[dict] = []

    def set(self, **kwargs) -> None:
        self.commands.append(kwargs)

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


class HomeAssistantDeviceTests(unittest.TestCase):
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
        self.light = FakeLight()
        self.ha = HomeAssistant(make_config(self.image_dir), self.light)
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

    def _config(self, component: str, entity: str) -> dict:
        payload = self._payloads(
            f"homeassistant/{component}/inky/{entity}/config"
        )[-1]
        if payload is None:
            return {}
        return json.loads(payload)

    def test_device_has_only_the_requested_entities(self) -> None:
        light = self._config("light", "light")
        self.assertEqual(light["name"], "Light")
        self.assertEqual(light["availability_mode"], "all")
        self.assertEqual(
            light["availability"][-1],
            {
                "topic": "inky/photo/controls",
                "payload_available": "idle",
                "payload_not_available": "busy",
            },
        )

        current = self._config("sensor", "current_photo")
        self.assertEqual(current["name"], "Current Photo")
        self.assertEqual(current["state_topic"], "inky/photo/displayed")
        self.assertNotIn("availability_mode", current)

        photo = self._config("select", "displayed_photo")
        self.assertEqual(photo["name"], "Photo")
        self.assertEqual(
            photo["options"],
            [photo_label(self.newer), photo_label(self.older)],
        )
        self.assertEqual(photo["availability_mode"], "all")

        busy = self._config("binary_sensor", "photo_busy")
        self.assertEqual(busy["name"], "Photo Busy")
        self.assertEqual(busy["icon"], "mdi:image-sync")
        self.assertEqual(busy["entity_category"], "diagnostic")
        self.assertEqual(busy["payload_on"], "busy")
        self.assertEqual(busy["payload_off"], "idle")
        self.assertNotIn("availability_mode", busy)

        self.assertEqual(self._payloads("inky/photo/controls")[-1], "idle")
        self.assertEqual(self._payloads("inky/photo/displayed")[-1], photo_label(self.newer))
        self.assertEqual(self._payloads("homeassistant/image/inky/latest_photo/config")[-1], None)
        self.assertEqual(self._payloads("homeassistant/sensor/inky/photo_library/config")[-1], None)
        self.assertEqual(
            self._payloads(f"homeassistant/image/inky/{photo_object_id(self.newer)}/config")[-1],
            None,
        )

    def test_select_updates_current_photo_then_goes_busy(self) -> None:
        self.client.published.clear()
        self.ha._handle_select_command(photo_label(self.older).encode())

        self.assertEqual(self.queued, [self.older])
        self.assertEqual(self._payloads("inky/photo/displayed")[-1], photo_label(self.older))
        self.assertEqual(
            json.loads(self._payloads("inky/photo/displayed_attrs")[-1])["filename"],
            "1700000000.png",
        )
        self.assertEqual(self._payloads("inky/photo/controls")[-1], "busy")

    def test_select_command_accepts_filename(self) -> None:
        self.ha._handle_select_command(b"1700000000.png")

        self.assertEqual(self.queued, [self.older])

    def test_busy_controls_ignore_photo_and_light_changes(self) -> None:
        self.ha.set_photo_controls_busy()
        self.client.published.clear()
        self.ha._handle_select_command(photo_label(self.older).encode())
        self.ha._handle_light_command(b'{"state": "ON", "brightness": 255}')

        self.assertEqual(self.queued, [])
        self.assertEqual(self.light.commands, [])
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

    def test_showing_a_stored_photo_clears_the_target(self) -> None:
        self.ha._handle_select_command(photo_label(self.older).encode())
        self.client.published.clear()
        self.ha.show_stored_photo(self.older)

        self.assertIsNone(self.ha._target)
        self.assertEqual(self._payloads("inky/photo/displayed")[-1], photo_label(self.older))

    def test_failed_show_reverts_current_photo(self) -> None:
        self.ha._handle_select_command(photo_label(self.older).encode())
        self.client.published.clear()
        self.ha.publish_displayed_state()

        self.assertIsNone(self.ha._target)
        self.assertEqual(self._payloads("inky/photo/displayed")[-1], photo_label(self.newer))

    def test_reconnect_republishes_the_current_photo_name(self) -> None:
        save_displayed(self.image_dir, self.older)
        self.client.published.clear()
        self.ha._on_connect(self.client, None, None, 0, None)

        self.assertEqual(self._payloads("inky/photo/displayed")[-1], photo_label(self.older))
        self.assertEqual(self._payloads("inky/photo")[-1], None)


if __name__ == "__main__":
    unittest.main()
