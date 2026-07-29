#!/opt/homegate/venv/bin/python
"""Обновляет стоп-кадры камер для стартовой страницы."""

import sys
from pathlib import Path

sys.path.insert(0, "/opt/homegate/bot")
import snapshot  # noqa: E402

OUT = Path("/var/www/dashboards/snapshots")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cams = snapshot.list_cameras()
    ok = 0
    for cid in cams:
        data, caption = snapshot.grab(cid)
        if data:
            tmp = OUT / ("cam{}.jpg.tmp".format(cid))
            tmp.write_bytes(data)
            tmp.replace(OUT / ("cam{}.jpg".format(cid)))
            ok += 1
        else:
            print("cam{}: {}".format(cid, caption), file=sys.stderr)
    print("обновлено {}/{}".format(ok, len(cams)))


if __name__ == "__main__":
    main()
