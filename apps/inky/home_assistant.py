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
    encode_photo_thumbnail,
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
        self._choices: list[PhotoChoice] = []
        self._library_ids: set[str] = set()
        self._library_mtimes: dict[str, int] = {}
        self._library_types: dict[str, str] = {}
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
        self._publish_image(path, "photo", "Could not update the current Home Assistant photo")
        if announce:
            self._publish_capture_event(path)
        self.set_displayed(path)

    def set_displayed(self, path: Path) -> None:
        self._displayed = path
        try:
            save_displayed(self._config.image_dir, path)
        except OSError:
            LOGGER.warning("Could not persist the displayed photo")
        self._refresh_library()

    def show_stored_photo(self, path: Path) -> None:
        self._publish_image(
            path,
            "photo",
            "Could not update the current Home Assistant photo",
        )
        self.set_displayed(path)

    def publish_displayed_state(self) -> None:
        if self._displayed is not None:
            self._publish_image(
                self._displayed,
                "photo",
                "Could not update the current Home Assistant photo",
            )
        self._refresh_library()

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

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code != 0:
            LOGGER.error("MQTT connection failed: %s", reason_code)
            return

        self._connected = True
        client.subscribe(self._topic("light/set"), qos=1)
        client.subscribe(self._topic("photo/select"), qos=1)
        self._displayed = load_displayed(self._config.image_dir)
        self._library_ids = set()
        self._library_mtimes = {}
        self._library_types = {}
        self._publish_discovery()
        client.publish(self._topic("status"), "online", qos=1, retain=True)
        self._publish_light_state()
        self._publish_current_photo()
        self._publish_photo_controls()
        self._refresh_library()
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
            return
        self._choose_photo(path)

    def _choose_photo(self, path: Path) -> None:
        if self._photo_busy:
            LOGGER.info("Ignored photo selection while Inky is busy")
            return
        if self._displayed is not None and path == self._displayed:
            return
        if self._on_show_photo is None or not self._on_show_photo(path):
            LOGGER.info("Ignored photo selection while Inky is busy")
            return
        self._set_photo_busy(True)

    def _publish_discovery(self) -> None:
        assert self._client is not None
        device = self._device()
        availability = self._availability()
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
            # Entity name is "Current Photo"; HA prefixes the device name
            # → "Inky Current Photo". unique_id stays latest_photo so existing
            # image.inky_latest_photo entities keep their entity_id.
            "name": "Current Photo",
            "default_entity_id": f"image.{self._mqtt.device_id}_latest_photo",
            "unique_id": f"{self._mqtt.device_id}_latest_photo",
            "image_topic": self._topic("photo"),
            "content_type": "image/png",
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
            "device_class": "running",
            "entity_category": "diagnostic",
            "device": device,
            **availability,
        }
        library = {
            "name": "Photo Library",
            "default_entity_id": f"sensor.{self._mqtt.device_id}_photo_library",
            "unique_id": f"{self._mqtt.device_id}_photo_library",
            "state_topic": self._topic("photo/library"),
            "json_attributes_topic": self._topic("photo/library_attrs"),
            "icon": "mdi:image-multiple",
            "entity_category": "diagnostic",
            "device": device,
            **availability,
        }
        self._publish_config("light", "light", light)
        self._publish_config("image", "latest_photo", image)
        self._publish_config("binary_sensor", "photo_busy", photo_busy)
        self._publish_config("sensor", "photo_library", library)
        # Clear obsolete retained discovery from earlier designs.
        for component, entity in (
            ("select", "displayed_photo"),
            ("image", "photo_preview"),
            ("button", "previous_photo"),
            ("button", "next_photo"),
            ("light", "show_light"),
            ("image", "archive_queue"),
            ("sensor", "last_photo"),
            ("sensor", "display_status"),
        ):
            self._clear_discovery(component, entity)
        for suffix in (
            "photo/preview",
            "photo/displayed",
            "photo/displayed_attrs",
            "photo/name",
            "photo/transfer",
            "photo/archive",
            "display/status",
        ):
            self._client.publish(self._topic(suffix), None, qos=1, retain=True)

    def _refresh_library(self) -> None:
        self._choices = photo_choices(
            self._config.image_dir,
            self._config.photo_select_limit,
            include=self._displayed,
        )
        if not self._connected or self._client is None:
            return
        next_ids: dict[str, PhotoChoice] = {}
        for choice in self._choices:
            try:
                next_ids[photo_object_id(choice.path)] = choice
            except ValueError:
                LOGGER.warning("Skipped Home Assistant library photo %s", choice.path)
        for object_id in self._library_ids - next_ids.keys():
            self._clear_discovery("image", object_id)
            self._client.publish(
                self._topic(f"photo/library/{object_id}"),
                None,
                qos=1,
                retain=True,
            )
            self._library_mtimes.pop(object_id, None)
            self._library_types.pop(object_id, None)
        self._library_ids = set(next_ids)
        for object_id, choice in next_ids.items():
            self._publish_library_photo(object_id, choice)
        self._publish_library_index()

    def _publish_library_photo(self, object_id: str, choice: PhotoChoice) -> None:
        assert self._client is not None
        try:
            mtime = choice.path.stat().st_mtime_ns
        except OSError:
            LOGGER.warning("Could not read stored photo %s", choice.path)
            return
        content_type = self._library_types.get(object_id)
        payload = None
        if self._library_mtimes.get(object_id) != mtime:
            try:
                payload, content_type = encode_photo_thumbnail(choice.path)
            except OSError:
                LOGGER.warning("Could not publish Home Assistant thumbnail for %s", choice.path)
                return
            self._library_mtimes[object_id] = mtime
            self._library_types[object_id] = content_type
        if content_type is None:
            return
        self._publish_config(
            "image",
            object_id,
            {
                "name": choice.label,
                "default_entity_id": f"image.{self._mqtt.device_id}_{object_id}",
                "unique_id": f"{self._mqtt.device_id}_{object_id}",
                "image_topic": self._topic(f"photo/library/{object_id}"),
                "content_type": content_type,
                "entity_category": "diagnostic",
                "device": self._device(),
                **self._availability(),
            },
        )
        if payload is not None:
            self._publish_image(
                choice.path,
                f"photo/library/{object_id}",
                f"Could not publish Home Assistant thumbnail for {choice.path.name}",
                payload=payload,
            )

    def _publish_library_index(self) -> None:
        if self._client is None:
            return
        photos = []
        for choice in self._choices:
            try:
                object_id = photo_object_id(choice.path)
            except ValueError:
                continue
            photos.append(
                {
                    "filename": choice.path.name,
                    "label": choice.label,
                    "entity_id": f"image.{self._mqtt.device_id}_{object_id}",
                    "current": bool(
                        self._displayed is not None
                        and choice.path.name == self._displayed.name
                    ),
                }
            )
        self._client.publish(
            self._topic("photo/library"),
            str(len(photos)),
            qos=1,
            retain=True,
        )
        self._client.publish(
            self._topic("photo/library_attrs"),
            json.dumps({"photos": photos, "busy": self._photo_busy}),
            qos=1,
            retain=True,
        )

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

    def _publish_current_photo(self) -> None:
        path = self._displayed
        if path is None or not path.is_file():
            try:
                path = max(
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
            path,
            "photo",
            "Could not update the current Home Assistant photo",
        )

    def _publish_image(
        self,
        path: Path,
        suffix: str,
        warning: str,
        payload: bytes | None = None,
    ) -> None:
        if not self._connected or self._client is None or mqtt is None:
            return
        try:
            photo = self._client.publish(
                self._topic(suffix),
                path.read_bytes() if payload is None else payload,
                qos=1,
                retain=True,
            )
            if photo.rc != mqtt.MQTT_ERR_SUCCESS:
                LOGGER.warning("%s", warning)
        except (OSError, RuntimeError, ValueError):
            LOGGER.warning("%s", warning)

    def _set_photo_busy(self, busy: bool) -> None:
        self._photo_busy = busy
        self._publish_photo_controls()
        if self._connected:
            self._publish_library_index()

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
