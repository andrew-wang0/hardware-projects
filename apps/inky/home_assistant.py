from __future__ import annotations

from collections.abc import Callable
import json
import logging
from pathlib import Path
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from config import Config, MqttConfig
from hardware import ShowLight
from photos import (
    PhotoChoice,
    load_displayed,
    neighbor_photo,
    photo_choices,
    resolve_photo_choice,
    save_displayed,
)


LOGGER = logging.getLogger(__name__)


class HomeAssistant:
    def __init__(self, config: Config, light: ShowLight) -> None:
        self._config = config
        self._mqtt: MqttConfig = config.mqtt
        self._light = light
        self._connected = False
        self._client = None
        self._last_connect_warning = 0.0
        self._on_show_photo: Callable[[Path], bool] | None = None
        self._displayed: Path | None = load_displayed(config.image_dir)
        self._preview: Path | None = self._displayed
        self._choices: list[PhotoChoice] = []
        self._published_options: list[str] | None = None

        if not self._mqtt.host:
            return
        if mqtt is None:
            LOGGER.error("paho-mqtt is unavailable; continuing without Home Assistant")
            return

        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=self._mqtt.device_id,
            )
            if self._mqtt.username:
                client.username_pw_set(self._mqtt.username, self._mqtt.password)
            client.will_set(self._topic("status"), "offline", qos=1, retain=True)
            client.reconnect_delay_set(min_delay=1, max_delay=30)
            client.on_connect = self._on_connect
            client.on_connect_fail = self._on_connect_fail
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message
            self._client = client
        except Exception:
            LOGGER.exception("MQTT setup failed; continuing without Home Assistant")

    def set_display_handler(self, on_show_photo: Callable[[Path], bool]) -> None:
        self._on_show_photo = on_show_photo

    def start(self) -> None:
        if self._client is None:
            if not self._mqtt.host:
                LOGGER.info(
                    "Home Assistant MQTT is disabled; set MQTT_HOST to enable it"
                )
            return
        try:
            self._client.connect_async(self._mqtt.host, self._mqtt.port)
            self._client.loop_start()
        except Exception:
            LOGGER.exception("MQTT startup failed; continuing without Home Assistant")
            self._client.loop_stop()
            self._client = None

    def publish_photo(self, path: Path, *, announce: bool = True) -> None:
        self._publish_image(path, "photo", "Could not update the latest Home Assistant photo")
        if announce:
            self._publish_capture_event(path)
        self._preview = path
        self._publish_image(
            path,
            "photo/preview",
            "Could not update the Home Assistant photo preview",
        )
        self.set_displayed(path)

    def set_displayed(self, path: Path) -> None:
        self._displayed = path
        self._preview = path
        try:
            save_displayed(self._config.image_dir, path)
        except OSError:
            LOGGER.warning("Could not persist the displayed photo")
        self._refresh_select()

    def show_stored_photo(self, path: Path) -> None:
        self.set_displayed(path)

    def publish_displayed_state(self) -> None:
        self._preview = self._displayed
        if self._displayed is not None:
            self._publish_image(
                self._displayed,
                "photo/preview",
                "Could not update the Home Assistant photo preview",
            )
        self._refresh_select()

    def close(self) -> None:
        if self._client is None:
            return
        self._connected = False
        try:
            message = self._client.publish(
                self._topic("status"),
                "offline",
                qos=1,
                retain=True,
            )
            message.wait_for_publish(timeout=2)
        except (RuntimeError, ValueError):
            LOGGER.warning("Could not publish MQTT offline state")
        finally:
            try:
                self._client.disconnect()
            finally:
                self._client.loop_stop()

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code != 0:
            LOGGER.error("MQTT connection failed: %s", reason_code)
            return

        self._connected = True
        client.subscribe(self._topic("light/set"), qos=1)
        client.subscribe(self._topic("photo/select"), qos=1)
        client.subscribe(self._topic("photo/previous"), qos=1)
        client.subscribe(self._topic("photo/next"), qos=1)
        self._displayed = load_displayed(self._config.image_dir)
        self._preview = self._displayed
        self._published_options = None
        self._publish_discovery()
        client.publish(self._topic("status"), "online", qos=1, retain=True)
        self._publish_light_state()
        self._publish_latest_capture()
        if self._preview is not None:
            self._publish_image(
                self._preview,
                "photo/preview",
                "Could not update the Home Assistant photo preview",
            )
        LOGGER.info("Connected to Home Assistant MQTT at %s", self._mqtt.host)

    def _on_connect_fail(self, _client, _userdata) -> None:
        now = time.monotonic()
        if now - self._last_connect_warning >= 60:
            LOGGER.warning("Home Assistant MQTT is unavailable; continuing locally")
            self._last_connect_warning = now

    def _on_disconnect(
        self,
        _client,
        _userdata,
        _disconnect_flags,
        reason_code,
        _properties,
    ) -> None:
        self._connected = False
        if reason_code != 0:
            LOGGER.warning("MQTT disconnected: %s", reason_code)

    def _on_message(self, _client, _userdata, message) -> None:
        if message.topic == self._topic("light/set"):
            self._handle_light_command(message.payload)
        elif message.topic == self._topic("photo/select"):
            self._handle_select_command(message.payload)
        elif message.topic == self._topic("photo/previous"):
            self._handle_step(-1)
        elif message.topic == self._topic("photo/next"):
            self._handle_step(1)

    def _handle_light_command(self, payload: bytes) -> None:
        try:
            command = json.loads(payload)
            if not isinstance(command, dict):
                raise ValueError("command must be a JSON object")
            state = command.get("state")
            on = None if state is None else str(state).upper() == "ON"
            if state is not None and str(state).upper() not in {"ON", "OFF"}:
                raise ValueError("state must be ON or OFF")

            value = command.get("brightness")
            brightness = None if value is None else float(value) / 255
            transition = float(
                command.get(
                    "transition",
                    self._config.light_transition_seconds,
                )
            )
            self._light.set(
                on=on,
                brightness=brightness,
                transition=transition,
            )
            self._publish_light_state()
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            LOGGER.warning("Ignored invalid MQTT light command: %s", error)

    def _handle_select_command(self, payload: bytes) -> None:
        try:
            option = payload.decode("utf-8").strip()
            path = resolve_photo_choice(option, self._choices, self._config.image_dir)
        except (UnicodeDecodeError, ValueError) as error:
            LOGGER.warning("Ignored invalid displayed photo command: %s", error)
            self._refresh_select()
            return
        self._choose_photo(path)

    def _handle_step(self, delta: int) -> None:
        path = neighbor_photo(self._choices, self._preview or self._displayed, delta)
        if path is None:
            return
        self._choose_photo(path)

    def _choose_photo(self, path: Path) -> None:
        self._preview = path
        self._publish_image(
            path,
            "photo/preview",
            "Could not update the Home Assistant photo preview",
        )
        self._refresh_select()
        if self._displayed is not None and path == self._displayed:
            return
        if self._on_show_photo is None or not self._on_show_photo(path):
            LOGGER.info(
                "Inky is capturing; previewed %s without changing the panel",
                path.name,
            )

    def _publish_discovery(self) -> None:
        assert self._client is not None
        device = {
            "identifiers": [self._mqtt.device_id],
            "name": "Inky",
            "manufacturer": "Custom",
            "model": "Inky Impression 7.3",
        }
        availability = {
            "availability_topic": self._topic("status"),
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        light = {
            # Entity name is "Light"; HA prefixes the device name → "Inky Light".
            "name": "Light",
            "default_entity_id": f"light.{self._mqtt.device_id}_light",
            "unique_id": f"{self._mqtt.device_id}_light",
            "schema": "json",
            "command_topic": self._topic("light/set"),
            "state_topic": self._topic("light/state"),
            "brightness": True,
            "supported_color_modes": ["brightness"],
            "transition": True,
            "device": device,
            **availability,
        }
        image = {
            "name": "Latest Photo",
            "default_entity_id": f"image.{self._mqtt.device_id}_latest_photo",
            "unique_id": f"{self._mqtt.device_id}_latest_photo",
            "image_topic": self._topic("photo"),
            "content_type": "image/png",
            "device": device,
            **availability,
        }
        preview = {
            "name": "Photo Preview",
            "default_entity_id": f"image.{self._mqtt.device_id}_photo_preview",
            "unique_id": f"{self._mqtt.device_id}_photo_preview",
            "image_topic": self._topic("photo/preview"),
            "content_type": "image/png",
            "icon": "mdi:image",
            "device": device,
            **availability,
        }
        previous_photo = {
            "name": "Previous Photo",
            "default_entity_id": f"button.{self._mqtt.device_id}_previous_photo",
            "unique_id": f"{self._mqtt.device_id}_previous_photo",
            "command_topic": self._topic("photo/previous"),
            "payload_press": "PRESS",
            "icon": "mdi:skip-previous",
            "device": device,
            **availability,
        }
        next_photo = {
            "name": "Next Photo",
            "default_entity_id": f"button.{self._mqtt.device_id}_next_photo",
            "unique_id": f"{self._mqtt.device_id}_next_photo",
            "command_topic": self._topic("photo/next"),
            "payload_press": "PRESS",
            "icon": "mdi:skip-next",
            "device": device,
            **availability,
        }
        self._publish_config("light", "light", light)
        self._publish_config("image", "latest_photo", image)
        self._publish_config("image", "photo_preview", preview)
        self._publish_config("button", "previous_photo", previous_photo)
        self._publish_config("button", "next_photo", next_photo)
        self._refresh_select()
        # Clear obsolete retained discovery from earlier designs.
        for component, entity in (
            ("light", "show_light"),
            ("image", "archive_queue"),
            ("sensor", "last_photo"),
            ("sensor", "display_status"),
        ):
            topic = (
                f"{self._mqtt.discovery_prefix}/{component}/"
                f"{self._mqtt.device_id}/{entity}/config"
            )
            self._client.publish(topic, None, qos=1, retain=True)
        for suffix in (
            "photo/name",
            "photo/transfer",
            "photo/archive",
            "display/status",
        ):
            self._client.publish(self._topic(suffix), None, qos=1, retain=True)

    def _refresh_select(self) -> None:
        current = self._preview or self._displayed
        self._choices = photo_choices(
            self._config.image_dir,
            self._config.photo_select_limit,
            include=current,
        )
        if not self._connected or self._client is None:
            return
        options = [choice.label for choice in self._choices]
        if options != self._published_options:
            self._published_options = options
            self._publish_config("select", "displayed_photo", self._select_config())
        self._publish_displayed_state()

    def _select_config(self) -> dict:
        return {
            "name": "Displayed Photo",
            "default_entity_id": f"select.{self._mqtt.device_id}_displayed_photo",
            "unique_id": f"{self._mqtt.device_id}_displayed_photo",
            "command_topic": self._topic("photo/select"),
            "state_topic": self._topic("photo/displayed"),
            "json_attributes_topic": self._topic("photo/displayed_attrs"),
            "options": [choice.label for choice in self._choices],
            "icon": "mdi:image-album",
            "device": {
                "identifiers": [self._mqtt.device_id],
                "name": "Inky",
                "manufacturer": "Custom",
                "model": "Inky Impression 7.3",
            },
            "availability_topic": self._topic("status"),
            "payload_available": "online",
            "payload_not_available": "offline",
        }

    def _publish_config(self, component: str, entity: str, payload: dict) -> None:
        assert self._client is not None
        topic = (
            f"{self._mqtt.discovery_prefix}/{component}/"
            f"{self._mqtt.device_id}/{entity}/config"
        )
        self._client.publish(topic, json.dumps(payload), qos=1, retain=True)

    def _publish_light_state(self) -> None:
        if self._client is None:
            return
        on, brightness = self._light.state()
        payload = {
            "state": "ON" if on else "OFF",
            "brightness": round(brightness * 255),
            "color_mode": "brightness",
        }
        self._client.publish(
            self._topic("light/state"),
            json.dumps(payload),
            qos=1,
            retain=True,
        )

    def _publish_displayed_state(self) -> None:
        if self._client is None:
            return
        label = ""
        attrs: dict[str, str] = {}
        current = self._preview or self._displayed
        if current is not None:
            for choice in self._choices:
                if choice.path.name == current.name:
                    label = choice.label
                    attrs = {
                        "filename": choice.path.name,
                        "on_panel": str(
                            self._displayed is not None
                            and choice.path.name == self._displayed.name
                        ).lower(),
                    }
                    break
        self._client.publish(
            self._topic("photo/displayed"),
            label if label else "None",
            qos=1,
            retain=True,
        )
        self._client.publish(
            self._topic("photo/displayed_attrs"),
            json.dumps(attrs),
            qos=1,
            retain=True,
        )

    def _publish_latest_capture(self) -> None:
        try:
            latest = max(
                (
                    candidate
                    for candidate in self._config.image_dir.glob("*.png")
                    if candidate.is_file() and not candidate.name.startswith(".")
                ),
                key=lambda candidate: candidate.stat().st_mtime_ns,
            )
        except (OSError, ValueError):
            return
        self._publish_image(
            latest,
            "photo",
            "Could not update the latest Home Assistant photo",
        )

    def _publish_image(self, path: Path, suffix: str, warning: str) -> None:
        if not self._connected or self._client is None or mqtt is None:
            return
        try:
            photo = self._client.publish(
                self._topic(suffix),
                path.read_bytes(),
                qos=1,
                retain=True,
            )
            if photo.rc != mqtt.MQTT_ERR_SUCCESS:
                LOGGER.warning("%s", warning)
        except (OSError, RuntimeError, ValueError):
            LOGGER.warning("%s", warning)

    def _publish_capture_event(self, path: Path) -> None:
        if not self._connected or self._client is None or mqtt is None:
            return
        try:
            capture = self._client.publish(
                self._topic("photo/captured"),
                path.name,
                qos=1,
                retain=False,
            )
            if capture.rc != mqtt.MQTT_ERR_SUCCESS:
                LOGGER.warning("Could not publish the Inky capture event")
        except (RuntimeError, ValueError):
            LOGGER.warning("Could not publish the Inky capture event")

    def _topic(self, suffix: str) -> str:
        return f"{self._mqtt.topic_prefix}/{suffix}"
