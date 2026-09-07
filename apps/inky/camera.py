from __future__ import annotations

import time

from libcamera import Transform, controls
from PIL import Image
from picamera2 import Picamera2

from config import Config


class Camera:
    def __init__(self, config: Config) -> None:
        self._exposure_value = config.camera_exposure_value
        self._ae_settle_seconds = config.camera_ae_settle_seconds
        self._camera = Picamera2()
        self._running = False
        self._camera.configure(
            self._camera.create_still_configuration(
                main={"size": config.camera_size, "format": "RGB888"},
                transform=Transform(
                    hflip=config.camera_hflip,
                    vflip=config.camera_vflip,
                ),
                controls={
                    "AfMode": controls.AfModeEnum.Continuous,
                    "ExposureValue": self._exposure_value,
                },
                buffer_count=3,
            )
        )

    def start(self) -> None:
        self._camera.start()
        self._camera.set_controls({"ExposureValue": self._exposure_value})
        self._running = True

    def capture(self) -> Image.Image:
        # The capture light jumps to full power with the camera start, so give
        # auto-exposure time to adapt before keeping a frame.
        deadline = time.monotonic() + self._ae_settle_seconds
        while time.monotonic() < deadline:
            self._camera.capture_metadata()

        with self._camera.captured_request(flush=True) as request:
            frame = request.make_array("main")

        # Picamera2's RGB888 stream is BGR byte order in a NumPy array.
        return Image.fromarray(frame[:, :, ::-1].copy())

    def stop(self) -> None:
        if self._running:
            self._camera.stop()
            self._running = False

    def close(self) -> None:
        self.stop()
        self._camera.close()
