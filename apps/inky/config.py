from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


INKY_DISPLAY_PINS = {2, 3, 8, 9, 10, 11, 17, 22, 27}
INKY_BUTTON_PINS = {5, 6, 16, 24}
INKY_LED_PIN = 13


def _integer(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None else int(value)


def _number(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None else float(value)


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class MqttConfig:
    host: str | None
    port: int
    username: str | None
    password: str | None
    device_id: str
    topic_prefix: str
    discovery_prefix: str


@dataclass(frozen=True)
class Config:
    image_dir: Path
    capture_button_pin: int
    signal_led_pin: int
    signal_led_active_high: bool
    light_pwm_pin: int
    light_active_high: bool
    light_brightness: float
    light_pwm_frequency: float
    light_minimum_duty: float
    light_transition_seconds: float
    button_bounce_seconds: float
    camera_size: tuple[int, int]
    camera_hflip: bool
    camera_vflip: bool
    camera_exposure_value: float
    camera_ae_settle_seconds: float
    inky_saturation: float
    mqtt: MqttConfig


def load_config() -> Config:
    app_dir = Path(__file__).resolve().parent
    config = Config(
        image_dir=Path(os.getenv("INKY_IMAGE_DIR", app_dir / "images")).expanduser(),
        capture_button_pin=_integer("CAPTURE_BUTTON_PIN", 24),
        signal_led_pin=_integer("SIGNAL_LED_PIN", 12),
        signal_led_active_high=_boolean("SIGNAL_LED_ACTIVE_HIGH", True),
        light_pwm_pin=_integer("LIGHT_PWM_PIN", 18),
        light_active_high=_boolean("LIGHT_ACTIVE_HIGH", True),
        light_brightness=_number("LIGHT_BRIGHTNESS", 1.0),
        light_pwm_frequency=_number("LIGHT_PWM_FREQUENCY", 1_000.0),
        light_minimum_duty=_number("LIGHT_MINIMUM_DUTY", 0.20),
        light_transition_seconds=_number("LIGHT_TRANSITION_SECONDS", 1.0),
        button_bounce_seconds=_number("BUTTON_BOUNCE_SECONDS", 0.08),
        camera_size=(
            _integer("CAMERA_WIDTH", 2304),
            _integer("CAMERA_HEIGHT", 1296),
        ),
        camera_hflip=_boolean("CAMERA_HFLIP", True),
        camera_vflip=_boolean("CAMERA_VFLIP", False),
        camera_exposure_value=_number("CAMERA_EXPOSURE_VALUE", 0.0),
        camera_ae_settle_seconds=_number("CAMERA_AE_SETTLE_SECONDS", 0.5),
        inky_saturation=_number("INKY_SATURATION", 0.5),
        mqtt=MqttConfig(
            host=os.getenv("MQTT_HOST"),
            port=_integer("MQTT_PORT", 1883),
            username=os.getenv("MQTT_USERNAME"),
            password=os.getenv("MQTT_PASSWORD"),
            device_id=os.getenv("MQTT_DEVICE_ID", "inky"),
            topic_prefix=os.getenv("MQTT_TOPIC_PREFIX", "inky").rstrip("/"),
            discovery_prefix=os.getenv(
                "MQTT_DISCOVERY_PREFIX",
                "homeassistant",
            ).rstrip("/"),
        ),
    )

    pins = {
        config.capture_button_pin,
        config.signal_led_pin,
        config.light_pwm_pin,
        INKY_LED_PIN,
    }
    if len(pins) != 4:
        raise ValueError(
            "The button, external signal LED, Pimoroni LED, and show light "
            "need different pins"
        )
    if any(pin < 0 or pin > 27 for pin in pins):
        raise ValueError("GPIO pins must be BCM numbers from 0 through 27")
    if pins & INKY_DISPLAY_PINS:
        raise ValueError("Configured GPIO conflicts with the Inky Impression")
    if {config.signal_led_pin, config.light_pwm_pin} & INKY_BUTTON_PINS:
        raise ValueError("An output cannot use an Inky Impression button pin")
    if not 0.0 <= config.light_brightness <= 1.0:
        raise ValueError("LIGHT_BRIGHTNESS must be between 0.0 and 1.0")
    if not 0.1 <= config.light_pwm_frequency <= 10_000:
        raise ValueError("LIGHT_PWM_FREQUENCY must be between 0.1 and 10000")
    if not 0.0 <= config.light_minimum_duty < 1.0:
        raise ValueError("LIGHT_MINIMUM_DUTY must be at least 0.0 and below 1.0")
    if config.light_transition_seconds < 0:
        raise ValueError("LIGHT_TRANSITION_SECONDS cannot be negative")
    if config.button_bounce_seconds < 0:
        raise ValueError("BUTTON_BOUNCE_SECONDS cannot be negative")
    if any(value <= 0 or value % 2 for value in config.camera_size):
        raise ValueError("Camera dimensions must be positive even numbers")
    if not -8.0 <= config.camera_exposure_value <= 8.0:
        raise ValueError("CAMERA_EXPOSURE_VALUE must be between -8.0 and 8.0")
    if config.camera_ae_settle_seconds < 0:
        raise ValueError("CAMERA_AE_SETTLE_SECONDS cannot be negative")
    if not 0.0 <= config.inky_saturation <= 1.0:
        raise ValueError("INKY_SATURATION must be between 0.0 and 1.0")
    if not 1 <= config.mqtt.port <= 65_535:
        raise ValueError("MQTT_PORT must be between 1 and 65535")
    if not config.mqtt.device_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("MQTT_DEVICE_ID may contain letters, numbers, - and _")
    if not config.mqtt.topic_prefix or not config.mqtt.discovery_prefix:
        raise ValueError("MQTT topic prefixes cannot be empty")
    if any(
        char in config.mqtt.topic_prefix + config.mqtt.discovery_prefix
        for char in "#+"
    ):
        raise ValueError("MQTT topic prefixes cannot contain wildcards")

    return config
