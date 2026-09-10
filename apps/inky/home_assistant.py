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
    photo_choices,
    photo_object_id,
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
        self._target: Path | None = None
        self._choices: list[PhotoChoice] = []
        self._published_options: list[str] | None = None
        self._photo_busy = False

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
        if announce:
            self._publish_capture_event(path)
        self.set_displayed(path)

    def set_displayed(self, path: Path) -> None:
        self._displayed = path
        self._target = None
        try:
            save_displayed(self._config.image_dir, path)
        except OSError:
            LOGGER.warning("Could not persist the displayed photo")
        self._refresh_select()

    def show_stored_photo(self, path: Path) -> None:
        self.set_displayed(path)

    def publish_displayed_state(self) -> None:
        self._target = None
        self._refresh_select()

    def set_photo_controls_busy(self) -> None:
        self._set_photo_busy(True)

    def set_photo_controls_idle(self) -> None:
        self._set_photo_busy(False)

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

    def _current_path(self) -> Path | None:
        return self._target or self._displayed

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code != 0:
            LOGGER.error("MQTT connection failed: %s", reason_code)
            return

        self._connected = True
        client.subscribe(self._topic("light/set"), qos=1)
        client.subscribe(self._topic("photo/select"), qos=1)
        self._displayed = load_displayed(self._config.image_dir)
        self._target = None
        self._published_options = None
        self._publish_photo_controls()
        self._publish_discovery()
        client.publish(self._topic("status"), "online", qos=1, retain=True)
        self._publish_light_state()
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

    def _handle_light_command(self, payload: bytes) -> None:
        if self._photo_busy:
            LOGGER.info("Ignored light command while Inky is busy")
            return
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
            LOGGER.warning("Ignored invalid photo command: %s", error)
            self._refresh_select()
            return
        self._choose_photo(path)

    def _choose_photo(self, path: Path) -> None:
        if self._photo_busy:
            LOGGER.info("Ignored photo selection while Inky is busy")
            self._refresh_select()
            return
        if self._current_path() is not None and path == self._current_path():
            self._refresh_select()
            return
        if self._on_show_photo is None or not self._on_show_photo(path):
            LOGGER.info("Ignored photo selection while Inky is busy")
            self._refresh_select()
            return
        self._target = path
        self._refresh_select()
        self._set_photo_busy(True)

    def _publish_discovery(self) -> None:
        assert self._client is not None
        device = self._device()
        availability = self._availability()
        light = {
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
            **self._photo_control_availability(),
        }
        current_photo = {
            "name": "Current Photo",
            "default_entity_id": f"sensor.{self._mqtt.device_id}_current_photo",
            "unique_id": f"{self._mqtt.device_id}_current_photo",
            "state_topic": self._topic("photo/displayed"),
            "json_attributes_topic": self._topic("photo/displayed_attrs"),
            "icon": "mdi:image",
            "device": device,
            **availability,
        }
        photo_busy = {
            "name": "Photo Busy",
            "default_entity_id": f"binary_sensor.{self._mqtt.device_id}_photo_busy",
            "unique_id": f"{self._mqtt.device_id}_photo_busy",
            "state_topic": self._topic("photo/controls"),
            "payload_on": "busy",
            "payload_off": "idle",
            "entity_category": "diagnostic",
            "icon": "mdi:image-sync",
            "device": device,
            **availability,
        }
        self._publish_config("light", "light", light)
        self._publish_config("sensor", "current_photo", current_photo)
        self._publish_config("binary_sensor", "photo_busy", photo_busy)
        self._refresh_select()
        self._clear_obsolete_entities()

    def _refresh_select(self) -> None:
        self._choices = photo_choices(
            self._config.image_dir,
            self._config.photo_select_limit,
            include=self._current_path(),
        )
        if not self._connected or self._client is None:
            return
        options = [choice.label for choice in self._choices]
        if options != self._published_options:
            self._published_options = options
            self._publish_config("select", "displayed_photo", self._select_config())
        self._publish_photo_state()

    def _select_config(self) -> dict:
        return {
            "name": "Photo",
            "default_entity_id": f"select.{self._mqtt.device_id}_displayed_photo",
            "unique_id": f"{self._mqtt.device_id}_displayed_photo",
            "command_topic": self._topic("photo/select"),
            "state_topic": self._topic("photo/displayed"),
            "options": [choice.label for choice in self._choices],
            "icon": "mdi:image-album",
            "device": self._device(),
            **self._photo_control_availability(),
        }

    def _photo_control_availability(self) -> dict:
        return {
            "availability": [
                {
                    "topic": self._topic("status"),
                    "payload_available": "online",
                    "payload_not_available": "offline",
                },
                {
                    "topic": self._topic("photo/controls"),
                    "payload_available": "idle",
                    "payload_not_available": "busy",
                },
            ],
            "availability_mode": "all",
        }

    def _publish_photo_state(self) -> None:
        if self._client is None:
            return
        current = self._current_path()
        label = ""
        attrs: dict[str, str] = {}
        if current is not None:
            for choice in self._choices:
                if choice.path.name == current.name:
                    label = choice.label
                    attrs = {"filename": choice.path.name}
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

    def _clear_obsolete_entities(self) -> None:
        assert self._client is not None
        for choice in self._choices:
            try:
                object_id = photo_object_id(choice.path)
            except ValueError:
                continue
            self._clear_discovery("image", object_id)
            self._client.publish(
                self._topic(f"photo/library/{object_id}"),
                None,
                qos=1,
                retain=True,
            )
        for component, entity in (
            ("image", "latest_photo"),
            ("image", "photo_preview"),
            ("button", "previous_photo"),
            ("button", "next_photo"),
            ("sensor", "photo_library"),
            ("light", "show_light"),
            ("image", "archive_queue"),
            ("sensor", "last_photo"),
            ("sensor", "display_status"),
        ):
            self._clear_discovery(component, entity)
        for suffix in (
            "photo",
            "photo/preview",
            "photo/library",
            "photo/library_attrs",
            "photo/name",
            "photo/transfer",
            "photo/archive",
            "display/status",
        ):
            self._client.publish(self._topic(suffix), None, qos=1, retain=True)

    def _device(self) -> dict:
        return {
            "identifiers": [self._mqtt.device_id],
            "name": "Inky",
            "manufacturer": "Custom",
            "model": "Inky Impression 7.3",
        }

    def _availability(self) -> dict:
        return {
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

    def _clear_discovery(self, component: str, entity: str) -> None:
        assert self._client is not None
        topic = (
            f"{self._mqtt.discovery_prefix}/{component}/"
            f"{self._mqtt.device_id}/{entity}/config"
        )
        self._client.publish(topic, None, qos=1, retain=True)

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

    def _set_photo_busy(self, busy: bool) -> None:
        self._photo_busy = busy
        self._publish_photo_controls()

    def _publish_photo_controls(self) -> None:
        if self._client is None:
            return
        self._client.publish(
            self._topic("photo/controls"),
            "busy" if self._photo_busy else "idle",
            qos=1,
            retain=True,
        )

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
