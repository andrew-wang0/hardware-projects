#!/usr/bin/env python3
from __future__ import annotations

import logging
from queue import SimpleQueue
import signal
import threading

from app import InkyApp
from camera import Camera
from config import load_config
from display import InkyDisplay
from hardware import ButtonEvent, CaptureButton, ShowLight, SignalLed, use_lgpio
from home_assistant import HomeAssistant


LOGGER = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    config.image_dir.mkdir(parents=True, exist_ok=True)
    stop_event = threading.Event()
    events: SimpleQueue[ButtonEvent] = SimpleQueue()

    def stop(_signum=None, _frame=None) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    controls = None
    camera = None
    show_light = None
    signal_led = None
    home_assistant = None
    try:
        use_lgpio()
        display = InkyDisplay(config)
        show_light = ShowLight(config)
        signal_led = SignalLed(config)
        controls = CaptureButton(config, events.put)
        camera = Camera(config)
        home_assistant = HomeAssistant(config, show_light)
        app = InkyApp(
            camera,
            display,
            controls,
            signal_led,
            show_light,
            events,
            stop_event,
            config.image_dir,
            home_assistant.publish_photo,
            home_assistant.show_stored_photo,
            home_assistant.publish_displayed_state,
        )
        home_assistant.set_display_handler(app.queue_stored_photo)
        home_assistant.start()
        app.run()
    finally:
        for resource in (
            controls,
            camera,
            signal_led,
            home_assistant,
            show_light,
        ):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    LOGGER.exception("Could not close %s", type(resource).__name__)


if __name__ == "__main__":
    main()
