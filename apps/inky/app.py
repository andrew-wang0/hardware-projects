from __future__ import annotations

from collections.abc import Callable
import logging
from pathlib import Path
from queue import Empty, SimpleQueue
import threading
import time

from PIL import Image

from camera import Camera
from display import InkyDisplay
from hardware import ButtonEvent, CaptureButton, ShowLight, SignalLed


LOGGER = logging.getLogger(__name__)


class InkyApp:
    def __init__(
        self,
        camera: Camera,
        display: InkyDisplay,
        controls: CaptureButton,
        signal_led: SignalLed,
        show_light: ShowLight,
        events: SimpleQueue[ButtonEvent],
        stop_event: threading.Event,
        image_dir: Path,
        on_photo: Callable[[Path], None],
        on_displayed: Callable[[Path], None],
        on_display_idle: Callable[[], None],
        on_busy: Callable[[], None],
        on_idle: Callable[[], None],
    ) -> None:
        self._camera = camera
        self._display = display
        self._controls = controls
        self._signal_led = signal_led
        self._show_light = show_light
        self._events = events
        self._stop_event = stop_event
        self._image_dir = image_dir
        self._on_photo = on_photo
        self._on_displayed = on_displayed
        self._on_display_idle = on_display_idle
        self._on_busy = on_busy
        self._on_idle = on_idle
        self._lock = threading.Lock()
        self._busy = False
        self._capturing = False
        self._pending_stored_photo: Path | None = None

    def queue_stored_photo(self, path: Path) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._pending_stored_photo = path
            return True

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                event = self._events.get(timeout=0.1)
            except Empty:
                path = self._take_pending_stored_photo()
                if path is not None:
                    self._show_stored(path)
                continue

            if event is ButtonEvent.PRESSED:
                self._prepare_capture()
            elif event is ButtonEvent.RELEASED:
                self._capture_and_show()

    def _take_pending_stored_photo(self) -> Path | None:
        with self._lock:
            if self._busy:
                return None
            path = self._pending_stored_photo
            if path is None:
                return None
            self._pending_stored_photo = None
            self._busy = True
            return path

    def _prepare_capture(self) -> None:
        with self._lock:
            self._busy = True
            self._capturing = True
            self._pending_stored_photo = None
        self._notify_busy()
        self._signal_led.on()
        self._show_light.start_capture()
        try:
            self._camera.start()
        except Exception:
            LOGGER.exception("Camera start failed")
            self._signal_led.off()
            self._show_light.stop_capture()
            self._controls.set_enabled(False)
            self._discard_events()
            self._set_idle()
            self._controls.set_enabled(True)

    def _capture_and_show(self) -> None:
        self._controls.set_enabled(False)
        with self._lock:
            self._busy = True
            self._capturing = True
            self._pending_stored_photo = None
        self._notify_busy()
        try:
            try:
                image = self._camera.capture()
            except Exception:
                LOGGER.exception("Photo capture failed")
                self._show_light.stop_capture()
                return
            finally:
                self._signal_led.off()
                try:
                    self._camera.stop()
                except Exception:
                    LOGGER.exception("Camera stop failed")

            self._show_light.start_busy()
            try:
                prepared = self._display.prepare(image)
                try:
                    path = self._store(prepared)
                except Exception:
                    LOGGER.exception("Image storage failed")
                else:
                    try:
                        self._on_photo(path)
                    except Exception:
                        LOGGER.exception("Home Assistant photo publication failed")
                self._display.show(prepared)
            finally:
                self._show_light.stop_busy()
        except Exception:
            LOGGER.exception("Inky image preparation or display update failed")
            self._show_light.stop_capture()
        finally:
            self._discard_events()
            self._set_idle()
            self._controls.set_enabled(True)

    def _show_stored(self, path: Path) -> None:
        self._controls.set_enabled(False)
        with self._lock:
            self._busy = True
        self._notify_busy()
        try:
            self._show_light.start_busy()
            try:
                with Image.open(path) as image:
                    prepared = self._display.prepare(image)
                try:
                    self._on_displayed(path)
                except Exception:
                    LOGGER.exception("Home Assistant displayed photo update failed")
                self._display.show(prepared)
                LOGGER.info("Displayed stored photo %s", path)
            finally:
                self._show_light.stop_busy()
        except Exception:
            LOGGER.exception("Stored photo display failed")
            try:
                self._on_display_idle()
            except Exception:
                LOGGER.exception("Home Assistant displayed photo revert failed")
            self._show_light.stop_capture()
        finally:
            self._discard_events()
            self._set_idle()
            self._controls.set_enabled(True)

    def _set_idle(self) -> None:
        with self._lock:
            self._busy = False
            self._capturing = False
        self._notify_idle()

    def _notify_busy(self) -> None:
        try:
            self._on_busy()
        except Exception:
            LOGGER.exception("Home Assistant busy update failed")

    def _notify_idle(self) -> None:
        try:
            self._on_idle()
        except Exception:
            LOGGER.exception("Home Assistant idle update failed")

    def _discard_events(self) -> None:
        while True:
            try:
                self._events.get_nowait()
            except Empty:
                return

    def _store(self, image: Image.Image) -> Path:
        path = self._image_dir / f"{int(time.time())}.png"
        image.save(path, format="PNG")
        LOGGER.info("Stored %s", path)
        return path
