from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from decoupled_wbc.control.teleop.device.quest.quest_bridge import QuestBridgeServer


def main():
    parser = argparse.ArgumentParser(description="Run the Meta Quest WebXR teleoperation bridge.")
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Network interface to bind the HTTPS bridge server to.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="HTTPS port used by the Quest browser and the GR00T Quest streamer.",
    )
    parser.add_argument(
        "--public-host",
        default="127.0.0.1",
        help="LAN hostname or IP address that the Quest browser should open.",
    )
    parser.add_argument(
        "--stale-after-seconds",
        type=float,
        default=0.5,
        help="Mark headset/controller data stale after this many seconds without updates.",
    )
    parser.add_argument(
        "--cert-path",
        type=Path,
        default=Path(".quest_bridge/cert.pem"),
        help="Path to the HTTPS certificate used for the WebXR page.",
    )
    parser.add_argument(
        "--key-path",
        type=Path,
        default=Path(".quest_bridge/key.pem"),
        help="Path to the HTTPS private key used for the WebXR page.",
    )
    parser.add_argument(
        "--no-auto-generate-cert",
        action="store_true",
        help="Disable automatic self-signed certificate generation.",
    )
    args = parser.parse_args()

    server = QuestBridgeServer(
        host=args.host,
        port=args.port,
        public_host=args.public_host,
        stale_after_seconds=args.stale_after_seconds,
        cert_path=args.cert_path,
        key_path=args.key_path,
        auto_generate_cert=not args.no_auto_generate_cert,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Quest bridge...")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
