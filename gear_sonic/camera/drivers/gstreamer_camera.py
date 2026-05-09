"""UDP H264 camera driver using ffmpeg subprocess.

Receives an MPEG-TS/H264 stream from the G1 Jetson via UDP and feeds frames
into the ComposedCameraServer for VLA data collection.

Uses ffmpeg as a subprocess piping raw RGB24 frames — does NOT require
OpenCV GStreamer support or PyAV MPEG-TS parsing (both are unreliable for UDP).

Jetson side (run on the robot's onboard computer):
    gst-launch-1.0 v4l2src device=/dev/video4 \\
        ! videoconvert ! videoscale \\
        ! video/x-raw,width=640,height=480,format=I420 \\
        ! x264enc tune=zerolatency \\
        ! h264parse ! mpegtsmux \\
        ! udpsink host=<PC_IP> port=5000

PC side (handled by this driver automatically via composed_camera --ego-view-camera gstreamer):
    device_id = UDP port (default "5000")
"""

import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

try:
    import gymnasium as gym
except ImportError:
    gym = None  # type: ignore[assignment]

from gear_sonic.camera.sensor import Sensor
from gear_sonic.camera.sensor_server import CameraMountPosition


@dataclass
class GStreamerCameraConfig:
    image_dim: tuple = (640, 480)
    fps: int = 30
    port: int = 5000


class GStreamerCameraSensor(Sensor):
    """Reads an MPEG-TS/H264 UDP stream via ffmpeg subprocess piping raw RGB24 frames."""

    def __init__(
        self,
        config: GStreamerCameraConfig = GStreamerCameraConfig(),
        mount_position: str = CameraMountPosition.EGO_VIEW.value,
        port: int | None = None,
    ):
        self.config = config
        self.mount_position = mount_position
        self._udp_port = port if port is not None else config.port
        self.image_dim = config.image_dim
        w, h = self.image_dim
        self._frame_bytes = w * h * 3  # RGB24

        self._latest_frame: np.ndarray | None = None
        self._lock = threading.Lock()
        self._running = True
        self._proc: subprocess.Popen | None = None

        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

        # Block until first frame arrives (up to 15s)
        deadline = time.time() + 15.0
        while time.time() < deadline:
            with self._lock:
                if self._latest_frame is not None:
                    break
            time.sleep(0.1)
        else:
            self._running = False
            raise RuntimeError(
                f"[{mount_position}] No frame received within 15s on UDP port {self._udp_port}. "
                "Check that the Jetson GStreamer pipeline is running and UDP packets arrive."
            )

        print(f"[{mount_position}] Camera ready on UDP port {self._udp_port}")

    def _start_ffmpeg(self) -> subprocess.Popen:
        w, h = self.image_dim
        url = f"udp://0.0.0.0:{self._udp_port}?reuse=1&timeout=5000000"
        cmd = [
            "ffmpeg",
            "-loglevel", "error",
            "-fflags", "nobuffer+discardcorrupt",
            "-flags", "low_delay",
            "-err_detect", "ignore_err",
            "-i", url,
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-vf", f"scale={w}:{h}",
            "-",
        ]
        print(f"[{self.mount_position}] Starting ffmpeg: {' '.join(cmd)}")
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _capture_loop(self) -> None:
        """Self-reconnecting ffmpeg capture loop."""
        w, h = self.image_dim
        while self._running:
            try:
                self._proc = self._start_ffmpeg()
                while self._running:
                    raw = self._proc.stdout.read(self._frame_bytes)
                    if len(raw) != self._frame_bytes:
                        # ffmpeg exited or truncated — break to reconnect
                        break
                    img = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 3))
                    with self._lock:
                        self._latest_frame = img.copy()
            except Exception as exc:
                if not self._running:
                    return
                print(f"[{self.mount_position}] Capture error: {exc}")
            finally:
                if self._proc is not None:
                    self._proc.kill()
                    self._proc = None
            if self._running:
                print(f"[{self.mount_position}] ffmpeg exited — reconnecting in 2s")
                time.sleep(2.0)

    def read(self) -> dict[str, Any] | None:
        with self._lock:
            if self._latest_frame is None:
                return None
            frame = self._latest_frame.copy()
        return {
            "timestamps": {self.mount_position: time.time()},
            "images": {self.mount_position: frame},
        }

    def serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        from gear_sonic.camera.sensor_server import ImageMessageSchema

        return ImageMessageSchema(
            timestamps=data["timestamps"], images=data["images"]
        ).serialize()

    def observation_space(self):
        if gym is None:
            return None
        w, h = self.image_dim
        return gym.spaces.Dict(
            {
                "color_image": gym.spaces.Box(
                    low=0, high=255, shape=(h, w, 3), dtype=np.uint8
                )
            }
        )

    def close(self) -> None:
        self._running = False
        if self._proc is not None:
            self._proc.kill()
            self._proc = None
