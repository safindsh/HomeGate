#!/usr/bin/env python3
"""Build anonymized HomeGate authorization-failure counters from nginx logs."""

import json
import os
import re
from datetime import datetime
from pathlib import Path

LOG_PATH = Path("/var/log/nginx/homegate.access.log")
ROTATED_LOG_PATH = Path("/var/log/nginx/homegate.access.log.1")
STATE_PATH = Path("/var/lib/homegate/auth-counter-state.json")
OUTPUT_PATH = Path("/var/www/dashboards/auth-stats.json")
FAILED_RE = re.compile(r'\[(?P<date>[^\]]+)\]\s+"[^"]+"\s+401\s')


def count_failures(data: bytes, today: str) -> tuple[int, int]:
    total = 0
    today_total = 0
    for match in FAILED_RE.finditer(data.decode("utf-8", errors="replace")):
        total += 1
        try:
            stamp = datetime.strptime(
                match.group("date").split()[0], "%d/%b/%Y:%H:%M:%S"
            )
            if stamp.date().isoformat() == today:
                today_total += 1
        except ValueError:
            continue
    return total, today_total


def read_from(path: Path, offset: int) -> tuple[bytes, int, int]:
    with path.open("rb") as stream:
        inode = os.fstat(stream.fileno()).st_ino
        stream.seek(min(offset, os.fstat(stream.fileno()).st_size))
        data = stream.read()
        return data, stream.tell(), inode


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> None:
    now = datetime.now().astimezone()
    today = now.date().isoformat()
    state = load_state()
    total = int(state.get("total", 0))
    daily = int(state.get("daily", 0)) if state.get("date") == today else 0
    old_inode = state.get("inode")
    old_offset = int(state.get("offset", 0))

    if not LOG_PATH.exists():
        return

    current_inode = LOG_PATH.stat().st_ino
    chunks = []
    if not old_inode:
        chunks.append(read_from(LOG_PATH, 0))
    elif current_inode == old_inode:
        chunks.append(read_from(LOG_PATH, old_offset))
    else:
        if ROTATED_LOG_PATH.exists() and ROTATED_LOG_PATH.stat().st_ino == old_inode:
            chunks.append(read_from(ROTATED_LOG_PATH, old_offset))
        chunks.append(read_from(LOG_PATH, 0))

    for data, _, _ in chunks:
        added_total, added_today = count_failures(data, today)
        total += added_total
        daily += added_today

    _, offset, inode = read_from(LOG_PATH, LOG_PATH.stat().st_size)
    state = {
        "total": total,
        "daily": daily,
        "date": today,
        "inode": inode,
        "offset": offset,
    }
    atomic_json(STATE_PATH, state)
    atomic_json(
        OUTPUT_PATH,
        {
            "today": daily,
            "total": total,
            "updated_at": now.isoformat(timespec="seconds"),
        },
    )


if __name__ == "__main__":
    main()
