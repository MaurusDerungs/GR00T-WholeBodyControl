from __future__ import annotations

import json
import ssl
from urllib.error import URLError
from urllib.request import urlopen


class QuestBridgeClient:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        timeout: float = 0.1,
    ):
        self.state_url = f"https://{host}:{port}/api/state"
        self.timeout = timeout
        self.ssl_context = ssl._create_unverified_context()

    def get_state(self) -> dict | None:
        try:
            with urlopen(
                self.state_url,
                timeout=self.timeout,
                context=self.ssl_context,
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except (TimeoutError, URLError, json.JSONDecodeError, ssl.SSLError):
            return None
