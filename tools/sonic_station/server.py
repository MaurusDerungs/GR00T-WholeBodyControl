#!/usr/bin/env python3
"""Minimal local backend for Sonic Station.

This backend intentionally uses only the Python standard library so it can run
inside the existing deploy environments without adding a web framework.
"""

from __future__ import annotations

import argparse
import base64
import os
import json
import mimetypes
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from motion_registry import REPO_ROOT, discover_motions, motions_payload

try:
    import msgpack
    import zmq
except ImportError:
    msgpack = None
    zmq = None


JOB_ROOT = REPO_ROOT / ".sonic_station" / "jobs"
GENERATE_SCRIPT = REPO_ROOT / "tools" / "sonic_station" / "generate_kimodo_reference.sh"
STREAM_SCRIPT = REPO_ROOT / "tools" / "sonic_station" / "stream_motion.py"
WEB_ROOT = REPO_ROOT / "tools" / "sonic_station" / "web"
_JOBS_LOCK = threading.Lock()
_JOBS: dict[str, "GenerationJob"] = {}
_PLAYBACK_LOCK = threading.Lock()
_PLAYBACKS: dict[str, "PlaybackJob"] = {}
_CAMERA_VIEW_STORE: "CameraViewStore | None" = None
_STATION_MODE = "sim"
_ROBOT_INTERFACE = ""


class CameraViewStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            self.write(self.default())

    @staticmethod
    def default() -> dict:
        return {"azimuth": 135.0, "elevation": -18.0, "distance": 3.0, "lookat": [0.0, 0.0, 0.75]}

    def read(self) -> dict:
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                data = self.default()
            return self._sanitize(data)

    def write(self, data: dict) -> dict:
        sanitized = self._sanitize(data)
        with self._lock:
            self.path.write_text(json.dumps(sanitized, separators=(",", ":")) + "\n", encoding="utf-8")
        return sanitized

    def _sanitize(self, data: dict) -> dict:
        default = self.default()
        try:
            azimuth = float(data.get("azimuth", default["azimuth"]))
            elevation = float(data.get("elevation", default["elevation"]))
            distance = float(data.get("distance", default["distance"]))
        except (TypeError, ValueError):
            azimuth = default["azimuth"]
            elevation = default["elevation"]
            distance = default["distance"]
        lookat_raw = data.get("lookat", default["lookat"])
        if not isinstance(lookat_raw, list) or len(lookat_raw) != 3:
            lookat = default["lookat"]
        else:
            try:
                lookat = [float(value) for value in lookat_raw]
            except (TypeError, ValueError):
                lookat = default["lookat"]
        return {
            "azimuth": azimuth % 360.0,
            "elevation": max(-80.0, min(20.0, elevation)),
            "distance": max(1.0, min(8.0, distance)),
            "lookat": [
                max(-2.0, min(2.0, lookat[0])),
                max(-2.0, min(2.0, lookat[1])),
                max(0.1, min(2.0, lookat[2])),
            ],
        }


class CameraFrameStore:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.enabled = msgpack is not None and zmq is not None
        self._lock = threading.Lock()
        self._latest_jpeg: bytes | None = None
        self._latest_camera = ""
        self._latest_timestamp = 0.0
        self._frames = 0
        self._errors = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def latest(self) -> tuple[bytes | None, str, float]:
        with self._lock:
            return self._latest_jpeg, self._latest_camera, self._latest_timestamp

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "host": self.host,
                "port": self.port,
                "frames": self._frames,
                "errors": self._errors,
                "camera": self._latest_camera,
                "timestamp": self._latest_timestamp,
                "has_frame": self._latest_jpeg is not None,
            }

    def _run(self) -> None:
        assert zmq is not None
        assert msgpack is not None
        context = zmq.Context()
        socket = context.socket(zmq.SUB)
        socket.setsockopt_string(zmq.SUBSCRIBE, "")
        socket.setsockopt(zmq.CONFLATE, 1)
        socket.setsockopt(zmq.RCVTIMEO, 250)
        socket.connect(f"tcp://{self.host}:{self.port}")
        try:
            while not self._stop.is_set():
                try:
                    raw = socket.recv()
                except zmq.Again:
                    continue
                try:
                    message = msgpack.unpackb(raw, raw=False)
                    images = message.get("images", {})
                    timestamps = message.get("timestamps", {})
                    if not images:
                        continue
                    camera = "ego_view" if "ego_view" in images else sorted(images.keys())[0]
                    payload = images[camera]
                    if isinstance(payload, str):
                        jpeg = base64.b64decode(payload)
                    elif isinstance(payload, bytes):
                        jpeg = payload
                    else:
                        continue
                    with self._lock:
                        self._latest_jpeg = jpeg
                        self._latest_camera = camera
                        self._latest_timestamp = float(timestamps.get(camera, time.time()))
                        self._frames += 1
                except Exception:
                    with self._lock:
                        self._errors += 1
        finally:
            socket.close(linger=0)
            context.term()


_CAMERA_STORE: CameraFrameStore | None = None


@dataclass
class GenerationJob:
    id: str
    prompt: str
    name: str
    status: str
    created_at: float
    updated_at: float
    duration_sec: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    returncode: int | None = None
    error: str | None = None
    log_path: str | None = None
    output_motion_path: str | None = None
    command: list[str] = field(default_factory=list)


@dataclass
class PlaybackJob:
    id: str
    motion_id: str
    motion_name: str
    motion_path: str
    status: str
    created_at: float
    updated_at: float
    started_at: float | None = None
    finished_at: float | None = None
    returncode: int | None = None
    error: str | None = None
    log_path: str | None = None
    command: list[str] = field(default_factory=list)


def _slugify(value: str, fallback: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", value.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("._-")
    return slug[:80] or fallback


def _job_payload(job: GenerationJob, include_log_tail: bool = True) -> dict:
    payload = asdict(job)
    payload["created_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(job.created_at))
    payload["updated_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(job.updated_at))
    if job.started_at is not None:
        payload["started_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(job.started_at))
    if job.finished_at is not None:
        payload["finished_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(job.finished_at))
    if include_log_tail and job.log_path:
        log_path = Path(job.log_path)
        if not log_path.is_absolute():
            log_path = REPO_ROOT / log_path
        if log_path.exists():
            lines = log_path.read_text(errors="replace").splitlines()
            payload["log_tail"] = lines[-80:]
    return payload


def _playback_payload(job: PlaybackJob, include_log_tail: bool = True) -> dict:
    payload = asdict(job)
    payload["created_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(job.created_at))
    payload["updated_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(job.updated_at))
    if job.started_at is not None:
        payload["started_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(job.started_at))
    if job.finished_at is not None:
        payload["finished_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(job.finished_at))
    if include_log_tail and job.log_path:
        log_path = Path(job.log_path)
        if not log_path.is_absolute():
            log_path = REPO_ROOT / log_path
        if log_path.exists():
            payload["log_tail"] = log_path.read_text(errors="replace").splitlines()[-80:]
    return payload


def _update_job(job_id: str, **updates: object) -> GenerationJob:
    with _JOBS_LOCK:
        job = _JOBS[job_id]
        for key, value in updates.items():
            setattr(job, key, value)
        job.updated_at = time.time()
        return job


def _update_playback(playback_id: str, **updates: object) -> PlaybackJob:
    with _PLAYBACK_LOCK:
        job = _PLAYBACKS[playback_id]
        for key, value in updates.items():
            setattr(job, key, value)
        job.updated_at = time.time()
        return job


def _resolve_motion_request(data: dict) -> tuple[str, str, str]:
    requested = str(data.get("motion_id") or data.get("motion") or data.get("name") or "").strip()
    if not requested:
        raise ValueError("motion_id is required")

    motions = discover_motions()
    for motion in motions:
        if requested in (motion.id, motion.name, motion.path):
            motion_path = Path(motion.path)
            if not motion_path.is_absolute():
                motion_path = REPO_ROOT / motion_path
            if not motion.valid:
                raise ValueError(f"motion is not valid: {motion.id}")
            return motion.id, motion.name, str(motion_path)

    direct_path = Path(requested)
    if not direct_path.is_absolute():
        direct_path = REPO_ROOT / direct_path
    if direct_path.exists():
        return f"path:{direct_path.name}", direct_path.name, str(direct_path)

    raise ValueError(f"motion not found: {requested}")


def _run_generation_job(job_id: str) -> None:
    with _JOBS_LOCK:
        job = _JOBS[job_id]
    log_path = Path(job.log_path or "")
    if not log_path.is_absolute():
        log_path = REPO_ROOT / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if not GENERATE_SCRIPT.exists():
        _update_job(
            job_id,
            status="failed",
            finished_at=time.time(),
            error=f"missing generation script: {GENERATE_SCRIPT}",
            returncode=127,
        )
        return

    env = os.environ.copy()
    if job.duration_sec is not None:
        env["KIMODO_DURATION"] = str(job.duration_sec)

    _update_job(job_id, status="running", started_at=time.time())
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(job.command)}\n\n")
        log.flush()
        proc = subprocess.run(
            job.command,
            cwd=REPO_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )

    output_path = REPO_ROOT / "gear_sonic_deploy" / "reference" / "kimodo" / job.name
    if proc.returncode == 0 and output_path.exists():
        _update_job(
            job_id,
            status="succeeded",
            finished_at=time.time(),
            returncode=proc.returncode,
            output_motion_path=str(output_path.relative_to(REPO_ROOT)),
        )
    else:
        error = f"generation failed with exit code {proc.returncode}"
        if proc.returncode == 0:
            error = f"generation finished but output motion is missing: {output_path}"
        _update_job(
            job_id,
            status="failed",
            finished_at=time.time(),
            returncode=proc.returncode,
            error=error,
        )


def _run_playback_job(playback_id: str) -> None:
    with _PLAYBACK_LOCK:
        job = _PLAYBACKS[playback_id]

    log_path = Path(job.log_path or "")
    if not log_path.is_absolute():
        log_path = REPO_ROOT / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if not STREAM_SCRIPT.exists():
        _update_playback(
            playback_id,
            status="failed",
            finished_at=time.time(),
            error=f"missing stream script: {STREAM_SCRIPT}",
            returncode=127,
        )
        return

    _update_playback(playback_id, status="running", started_at=time.time())
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(job.command)}\n\n")
        log.flush()
        proc = subprocess.run(
            job.command,
            cwd=REPO_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )

    if proc.returncode == 0:
        _update_playback(
            playback_id,
            status="succeeded",
            finished_at=time.time(),
            returncode=proc.returncode,
        )
    else:
        _update_playback(
            playback_id,
            status="failed",
            finished_at=time.time(),
            returncode=proc.returncode,
            error=f"playback stream failed with exit code {proc.returncode}",
        )


class SonicStationHandler(BaseHTTPRequestHandler):
    server_version = "SonicStation/0.2"

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send_json({"ok": False, "error": "not_found", "path": str(path)}, HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        content_type, _ = mimetypes.guess_type(str(path))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_jpeg(self, body: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _try_static(self, path: str) -> bool:
        if path == "/":
            self._send_file(WEB_ROOT / "index.html")
            return True
        if path.startswith("/static/"):
            relative = path.removeprefix("/static/").strip("/")
            candidate = (WEB_ROOT / relative).resolve()
            if WEB_ROOT.resolve() not in candidate.parents:
                self._send_json({"ok": False, "error": "bad_static_path"}, HTTPStatus.BAD_REQUEST)
                return True
            self._send_file(candidate)
            return True
        return False

    def _read_json(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def do_OPTIONS(self) -> None:
        self._send_json({"ok": True})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if self._try_static(path):
            return
        if path == "/health":
            self._send_json(
                {
                    "ok": True,
                    "service": "sonic-station",
                    "repo_root": str(REPO_ROOT),
                    "station_mode": _STATION_MODE,
                    "robot_interface": _ROBOT_INTERFACE,
                    "endpoints": [
                        "/health",
                        "/motions",
                        "/generate",
                        "/jobs",
                        "/jobs/<id>",
                        "/motion/play",
                        "/playbacks",
                        "/playbacks/<id>",
                        "/control/reset",
                        "/",
                        "/camera/status",
                        "/camera/latest.jpg",
                        "/camera/view",
                    ],
                }
            )
            return
        if path == "/camera/view":
            view = _CAMERA_VIEW_STORE.read() if _CAMERA_VIEW_STORE else CameraViewStore.default()
            self._send_json({"ok": True, "view": view})
            return
        if path == "/camera/status":
            status = _CAMERA_STORE.status() if _CAMERA_STORE else {"enabled": False}
            self._send_json({"ok": True, "camera": status})
            return
        if path == "/camera/latest.jpg":
            if _CAMERA_STORE is None:
                self._send_json({"ok": False, "error": "camera_not_configured"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            jpeg, _, _ = _CAMERA_STORE.latest()
            if jpeg is None:
                self._send_json({"ok": False, "error": "no_camera_frame"}, HTTPStatus.NOT_FOUND)
                return
            self._send_jpeg(jpeg)
            return
        if path == "/motions":
            self._send_json(motions_payload())
            return
        if path == "/jobs":
            with _JOBS_LOCK:
                jobs = sorted(_JOBS.values(), key=lambda item: item.created_at, reverse=True)
            self._send_json(
                {
                    "count": len(jobs),
                    "jobs": [_job_payload(job, include_log_tail=False) for job in jobs],
                }
            )
            return
        if path.startswith("/jobs/"):
            job_id = path.removeprefix("/jobs/").strip("/")
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
            if job is None:
                self._send_json({"ok": False, "error": "job_not_found", "job_id": job_id}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "job": _job_payload(job)})
            return
        if path == "/playbacks":
            with _PLAYBACK_LOCK:
                playbacks = sorted(_PLAYBACKS.values(), key=lambda item: item.created_at, reverse=True)
            self._send_json(
                {
                    "count": len(playbacks),
                    "playbacks": [_playback_payload(job, include_log_tail=False) for job in playbacks],
                }
            )
            return
        if path.startswith("/playbacks/"):
            playback_id = path.removeprefix("/playbacks/").strip("/")
            with _PLAYBACK_LOCK:
                job = _PLAYBACKS.get(playback_id)
            if job is None:
                self._send_json(
                    {"ok": False, "error": "playback_not_found", "playback_id": playback_id},
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._send_json({"ok": True, "playback": _playback_payload(job)})
            return
        self._send_json(
            {
                "ok": False,
                "error": "not_found",
                "path": path,
                "available": [
                    "/health",
                    "/motions",
                    "/generate",
                    "/jobs",
                    "/jobs/<id>",
                    "/motion/play",
                    "/control/reset",
                    "/playbacks",
                    "/playbacks/<id>",
                    "/camera/status",
                    "/camera/latest.jpg",
                    "/camera/view",
                ],
            },
            HTTPStatus.NOT_FOUND,
        )

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in ("/generate", "/motion/play", "/control/reset", "/camera/view"):
            self._send_json(
                {
                    "ok": False,
                    "error": "not_found",
                    "path": path,
                    "available": ["/generate", "/motion/play", "/control/reset", "/camera/view"],
                },
                HTTPStatus.NOT_FOUND,
            )
            return

        try:
            data = self._read_json()
        except ValueError as exc:
            self._send_json({"ok": False, "error": "bad_request", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if path == "/camera/view":
            if _CAMERA_VIEW_STORE is None:
                self._send_json({"ok": False, "error": "camera_view_not_configured"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            view = _CAMERA_VIEW_STORE.write(data)
            self._send_json({"ok": True, "view": view})
            return

        if path == "/control/reset":
            action = str(data.get("action", "")).strip()
            allowed_actions = {"emergency_stop", "idle_reset", "motion_restart"}
            if action not in allowed_actions:
                self._send_json(
                    {
                        "ok": False,
                        "error": "bad_request",
                        "message": f"action must be one of: {', '.join(sorted(allowed_actions))}",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            if not STREAM_SCRIPT.exists():
                self._send_json(
                    {"ok": False, "error": "missing_stream_script", "path": str(STREAM_SCRIPT)},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return

            command = [
                sys.executable,
                str(STREAM_SCRIPT),
                "--control-action",
                action,
                "--startup-delay",
                "0.1",
                "--repeat-command",
                "8",
                "--repeat-command-interval",
                "0.04",
            ]
            try:
                proc = subprocess.run(
                    command,
                    cwd=REPO_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=3.0,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                self._send_json(
                    {"ok": False, "error": "control_timeout", "action": action},
                    HTTPStatus.GATEWAY_TIMEOUT,
                )
                return
            if proc.returncode != 0:
                self._send_json(
                    {
                        "ok": False,
                        "error": "control_failed",
                        "action": action,
                        "returncode": proc.returncode,
                        "output": proc.stdout[-2000:],
                    },
                    HTTPStatus.BAD_GATEWAY,
                )
                return
            self._send_json({"ok": True, "action": action})
            return

        if path == "/motion/play":
            try:
                motion_id, motion_name, motion_path = _resolve_motion_request(data)
            except ValueError as exc:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": str(exc)},
                    HTTPStatus.BAD_REQUEST,
                )
                return

            try:
                startup_delay = float(data.get("startup_delay", 1.0))
            except (TypeError, ValueError):
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "startup_delay must be a number"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            if startup_delay < 0 or startup_delay > 30:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "startup_delay must be between 0 and 30 seconds"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            playback_id = uuid.uuid4().hex[:12]
            log_path = JOB_ROOT / f"playback_{playback_id}.log"
            command = [
                sys.executable,
                str(STREAM_SCRIPT),
                motion_path,
                "--startup-delay",
                str(startup_delay),
            ]
            motion_display_path = Path(motion_path)
            try:
                motion_display = str(motion_display_path.relative_to(REPO_ROOT))
            except ValueError:
                motion_display = str(motion_display_path)
            now = time.time()
            job = PlaybackJob(
                id=playback_id,
                motion_id=motion_id,
                motion_name=motion_name,
                motion_path=motion_display,
                status="queued",
                created_at=now,
                updated_at=now,
                log_path=str(log_path.relative_to(REPO_ROOT)),
                command=command,
            )
            with _PLAYBACK_LOCK:
                _PLAYBACKS[playback_id] = job
            thread = threading.Thread(target=_run_playback_job, args=(playback_id,), daemon=True)
            thread.start()
            self._send_json(
                {"ok": True, "playback": _playback_payload(job, include_log_tail=False)},
                HTTPStatus.ACCEPTED,
            )
            return

        prompt = str(data.get("prompt", "")).strip()
        if not prompt:
            self._send_json({"ok": False, "error": "bad_request", "message": "prompt is required"}, HTTPStatus.BAD_REQUEST)
            return

        requested_name = str(data.get("name", "")).strip()
        name = _slugify(requested_name or prompt, fallback="kimodo_motion")
        job_id = uuid.uuid4().hex[:12]
        name = f"{name}_{job_id[:6]}"

        duration_raw = data.get("duration")
        duration_sec = None
        if duration_raw not in (None, ""):
            try:
                duration_sec = float(duration_raw)
            except (TypeError, ValueError):
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "duration must be a number"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            if duration_sec <= 0 or duration_sec > 30:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "duration must be between 0 and 30 seconds"},
                    HTTPStatus.BAD_REQUEST,
                )
                return

        log_path = JOB_ROOT / f"{job_id}.log"
        command = [str(GENERATE_SCRIPT), prompt, name]
        now = time.time()
        job = GenerationJob(
            id=job_id,
            prompt=prompt,
            name=name,
            status="queued",
            created_at=now,
            updated_at=now,
            duration_sec=duration_sec,
            log_path=str(log_path.relative_to(REPO_ROOT)),
            command=command,
        )
        with _JOBS_LOCK:
            _JOBS[job_id] = job

        thread = threading.Thread(target=_run_generation_job, args=(job_id,), daemon=True)
        thread.start()
        self._send_json({"ok": True, "job": _job_payload(job, include_log_tail=False)}, HTTPStatus.ACCEPTED)

    def log_message(self, fmt: str, *args) -> None:
        message = fmt % args
        quiet_paths = (
            "GET /health ",
            "GET /motions ",
            "GET /jobs ",
            "GET /playbacks ",
            "GET /camera/status ",
            "GET /camera/latest.jpg",
            "GET /camera/view ",
            "POST /camera/view ",
            "POST /control/reset ",
        )
        if any(path in message for path in quiet_paths):
            return
        print(f"[sonic-station] {self.address_string()} - {message}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--station-mode", choices=("sim", "real"), default="sim")
    parser.add_argument("--robot-interface", default="")
    parser.add_argument("--camera-disabled", action="store_true")
    parser.add_argument("--camera-host", default="localhost")
    parser.add_argument("--camera-port", type=int, default=5555)
    parser.add_argument("--camera-view-file", type=Path, default=REPO_ROOT / ".sonic_station" / "camera_view.json")
    return parser.parse_args()


def main() -> None:
    global _CAMERA_STORE, _CAMERA_VIEW_STORE, _STATION_MODE, _ROBOT_INTERFACE
    args = parse_args()
    _STATION_MODE = args.station_mode
    _ROBOT_INTERFACE = args.robot_interface
    if not args.camera_disabled:
        _CAMERA_STORE = CameraFrameStore(args.camera_host, args.camera_port)
        _CAMERA_STORE.start()
    _CAMERA_VIEW_STORE = CameraViewStore(args.camera_view_file)
    server = ThreadingHTTPServer((args.host, args.port), SonicStationHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"Sonic Station backend running at {url}")
    print(f"  health:  {url}/health")
    print(f"  motions: {url}/motions")
    print(f"  jobs:    {url}/jobs")
    print(f"  mode:    {args.station_mode}{(' on ' + args.robot_interface) if args.robot_interface else ''}")
    if args.camera_disabled:
        print("  camera:  disabled")
    else:
        print(f"  camera:  tcp://{args.camera_host}:{args.camera_port} -> {url}/camera/latest.jpg")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Sonic Station backend.")
    finally:
        if _CAMERA_STORE is not None:
            _CAMERA_STORE.stop()
        server.server_close()


if __name__ == "__main__":
    main()
