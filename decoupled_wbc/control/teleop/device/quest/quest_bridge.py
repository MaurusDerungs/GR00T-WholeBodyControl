from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from typing import Any


WEB_ROOT = Path(__file__).resolve().parent / "web"
INDEX_HTML_PATH = WEB_ROOT / "index.html"


def _is_ip_address(value: str) -> bool:
    try:
        socket.inet_aton(value)
        return True
    except OSError:
        return False


def _as_float_list(values: Any, length: int, default: float = 0.0) -> list[float]:
    if not isinstance(values, list | tuple):
        return [default] * length

    coerced: list[float] = []
    for item in values[:length]:
        try:
            coerced.append(float(item))
        except (TypeError, ValueError):
            coerced.append(default)

    if len(coerced) < length:
        coerced.extend([default] * (length - len(coerced)))
    return coerced


def _sanitize_pose(raw_pose: Any) -> dict[str, list[float]] | None:
    if not isinstance(raw_pose, dict):
        return None

    position = _as_float_list(raw_pose.get("position"), 3)
    orientation = _as_float_list(raw_pose.get("orientation"), 4)

    if orientation == [0.0, 0.0, 0.0, 0.0]:
        orientation = [0.0, 0.0, 0.0, 1.0]

    return {
        "position": position,
        "orientation": orientation,
    }


def _sanitize_buttons(raw_buttons: Any, limit: int = 8) -> list[dict[str, float | bool]]:
    if not isinstance(raw_buttons, list):
        return []

    buttons: list[dict[str, float | bool]] = []
    for raw_button in raw_buttons[:limit]:
        if not isinstance(raw_button, dict):
            continue
        buttons.append(
            {
                "pressed": bool(raw_button.get("pressed", False)),
                "touched": bool(raw_button.get("touched", False)),
                "value": float(raw_button.get("value", 0.0)),
            }
        )
    return buttons


def _sanitize_profiles(raw_profiles: Any, limit: int = 8) -> list[str]:
    if not isinstance(raw_profiles, list):
        return []
    return [str(profile) for profile in raw_profiles[:limit]]


def _sanitize_controller(raw_controller: Any, handedness: str) -> dict[str, Any]:
    if not isinstance(raw_controller, dict):
        return {
            "handedness": handedness,
            "connected": False,
            "pose": None,
            "axes": [0.0, 0.0, 0.0, 0.0],
            "buttons": [],
            "profiles": [],
        }

    return {
        "handedness": handedness,
        "connected": bool(raw_controller.get("connected", False)),
        "pose": _sanitize_pose(raw_controller),
        "axes": _as_float_list(raw_controller.get("axes"), 4),
        "buttons": _sanitize_buttons(raw_controller.get("buttons")),
        "profiles": _sanitize_profiles(raw_controller.get("profiles")),
    }


class QuestBridgeState:
    def __init__(self, stale_after_seconds: float):
        self.stale_after_seconds = stale_after_seconds
        self._lock = threading.Lock()
        self._latest_state: dict[str, Any] = {
            "received_at": None,
            "received_age_sec": None,
            "stale": True,
            "session": {
                "active": False,
                "mode": "idle",
                "reference_space_type": "local-floor",
                "frame_id": 0,
                "client_time_ms": 0.0,
            },
            "headset": None,
            "controllers": {
                "left": _sanitize_controller(None, "left"),
                "right": _sanitize_controller(None, "right"),
            },
            "meta": {
                "source": "quest_webxr_bridge",
                "client_host": None,
            },
        }

    def update(self, payload: dict[str, Any], client_host: str):
        now = time.time()

        session = payload.get("session", {}) if isinstance(payload, dict) else {}
        headset = payload.get("headset", None) if isinstance(payload, dict) else None
        controllers = payload.get("controllers", {}) if isinstance(payload, dict) else {}

        sanitized_state = {
            "received_at": now,
            "received_age_sec": 0.0,
            "stale": False,
            "session": {
                "active": bool(session.get("active", False)),
                "mode": str(session.get("mode", "immersive-vr")),
                "reference_space_type": str(
                    session.get("reference_space_type", "local-floor")
                ),
                "frame_id": int(session.get("frame_id", 0)),
                "client_time_ms": float(session.get("client_time_ms", 0.0)),
            },
            "headset": _sanitize_pose(headset),
            "controllers": {
                "left": _sanitize_controller(controllers.get("left"), "left"),
                "right": _sanitize_controller(controllers.get("right"), "right"),
            },
            "meta": {
                "source": "quest_webxr_bridge",
                "client_host": client_host,
            },
        }

        with self._lock:
            self._latest_state = sanitized_state

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot = deepcopy(self._latest_state)

        received_at = snapshot.get("received_at")
        if received_at is None:
            snapshot["stale"] = True
            snapshot["received_age_sec"] = None
            return snapshot

        age = max(0.0, time.time() - float(received_at))
        snapshot["received_age_sec"] = age
        snapshot["stale"] = age > self.stale_after_seconds
        return snapshot


class QuestBridgeRequestHandler(BaseHTTPRequestHandler):
    bridge_state: QuestBridgeState
    index_html: str

    def _send_json(self, status_code: int, payload: dict[str, Any]):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html: str):
        data = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in {"/", "/index.html"}:
            self._send_html(self.index_html)
            return

        if self.path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "received_state": self.bridge_state.snapshot().get("received_at") is not None,
                },
            )
            return

        if self.path == "/api/state":
            self._send_json(HTTPStatus.OK, self.bridge_state.snapshot())
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Route not found")

    def do_POST(self):
        if self.path != "/api/update":
            self.send_error(HTTPStatus.NOT_FOUND, "Route not found")
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
            return

        client_host = self.client_address[0]
        self.bridge_state.update(payload, client_host=client_host)
        self._send_json(HTTPStatus.OK, {"ok": True})

    def log_message(self, format: str, *args: Any):
        # Keep bridge logs readable while still surfacing access information.
        print(f"[quest-bridge] {self.address_string()} - {format % args}")


def ensure_self_signed_cert(
    cert_path: Path,
    key_path: Path,
    public_hosts: list[str],
):
    if cert_path.exists() and key_path.exists():
        return

    openssl = shutil.which("openssl")
    if openssl is None:
        raise RuntimeError(
            "OpenSSL is required to generate a self-signed certificate. "
            "Install openssl or provide --cert-path/--key-path."
        )

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    alt_names: list[str] = [
        "DNS.1 = localhost",
        "IP.1 = 127.0.0.1",
    ]
    dns_index = 2
    ip_index = 2
    for host in public_hosts:
        if not host:
            continue
        if _is_ip_address(host):
            alt_names.append(f"IP.{ip_index} = {host}")
            ip_index += 1
        else:
            alt_names.append(f"DNS.{dns_index} = {host}")
            dns_index += 1

    openssl_config = f"""
[req]
default_bits = 2048
prompt = no
default_md = sha256
x509_extensions = v3_req
distinguished_name = dn

[dn]
CN = QuestTeleopLocal

[v3_req]
subjectAltName = @alt_names

[alt_names]
{chr(10).join(alt_names)}
""".strip()

    with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as config_file:
        config_file.write(openssl_config)
        config_path = Path(config_file.name)

    try:
        subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-nodes",
                "-newkey",
                "rsa:2048",
                "-keyout",
                str(key_path),
                "-out",
                str(cert_path),
                "-days",
                "3650",
                "-config",
                str(config_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Failed to generate TLS certificate with openssl: {exc.stderr.strip()}"
        ) from exc
    finally:
        config_path.unlink(missing_ok=True)


@dataclass
class QuestBridgeServer:
    host: str = "0.0.0.0"
    port: int = 8765
    public_host: str = "127.0.0.1"
    stale_after_seconds: float = 0.5
    cert_path: Path = Path(".quest_bridge/cert.pem")
    key_path: Path = Path(".quest_bridge/key.pem")
    auto_generate_cert: bool = True

    def __post_init__(self):
        if self.auto_generate_cert:
            ensure_self_signed_cert(
                cert_path=self.cert_path,
                key_path=self.key_path,
                public_hosts=[self.public_host],
            )

        if not INDEX_HTML_PATH.exists():
            raise FileNotFoundError(f"Quest bridge web app not found at {INDEX_HTML_PATH}")

        handler_cls = type(
            "ConfiguredQuestBridgeRequestHandler",
            (QuestBridgeRequestHandler,),
            {},
        )
        handler_cls.bridge_state = QuestBridgeState(self.stale_after_seconds)
        handler_cls.index_html = INDEX_HTML_PATH.read_text(encoding="utf-8")

        self._httpd = ThreadingHTTPServer((self.host, self.port), handler_cls)
        self._httpd.daemon_threads = True

        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(str(self.cert_path), str(self.key_path))
        self._httpd.socket = ssl_context.wrap_socket(self._httpd.socket, server_side=True)

    @property
    def url(self) -> str:
        return f"https://{self.public_host}:{self.port}/"

    def serve_forever(self):
        print("")
        print("Quest bridge ready.")
        print(f"  WebXR page : {self.url}")
        print(f"  API health : {self.url}api/health")
        print(f"  Cert path  : {self.cert_path}")
        print("")
        print("Open the WebXR page in the Meta Quest browser, accept the certificate warning,")
        print("and press 'Start VR Session' once the page loads.")
        print("")
        self._httpd.serve_forever()

    def shutdown(self):
        self._httpd.shutdown()
        self._httpd.server_close()
