#!/usr/bin/env python3
"""Generate a private go2rtc config from HomeGate's camera inventory."""

import json
import os
from pathlib import Path
from urllib.parse import quote


BOT_CONFIG = Path(os.getenv("HOMEGATE_BOT_CONFIG", "/opt/homegate/config/bot.json"))
OUTPUT = Path(os.getenv("GO2RTC_CONFIG", "/opt/go2rtc-homegate/go2rtc.yaml"))


def main():
    config = json.loads(BOT_CONFIG.read_text(encoding="utf-8"))
    username = quote(str(config["camera_user"]), safe="")
    password = quote(str(config["camera_pass"]), safe="")
    cameras = config.get("cameras", {})
    if not cameras:
        raise SystemExit("No cameras found in bot.json")

    sections = [
        'api:\n  listen: "127.0.0.1:1984"',
        'rtsp:\n  listen: "127.0.0.1:8554"',
        'webrtc:\n  listen: "127.0.0.1:8555/tcp"',
        "log:\n  level: info",
    ]
    streams = ["streams:"]
    for camera_id, camera in sorted(cameras.items(), key=lambda item: int(item[0])):
        streams.append(
            f'  cam{camera_id}: "rtsp://{username}:{password}'
            f'@{camera["ip"]}:554/stream2"'
        )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".yaml.tmp")
    temporary.write_text("\n\n".join(sections) + "\n\n" + "\n".join(streams) + "\n")
    temporary.chmod(0o600)
    temporary.replace(OUTPUT)
    print(f"Generated {OUTPUT} with {len(cameras)} camera streams")


if __name__ == "__main__":
    main()
