"""Снятие стоп-кадров с камер Tapo по RTSP."""

import io
import json
import logging
from pathlib import Path

import av

BOT_CONFIG = Path("/opt/homegate/config/bot.json")
log = logging.getLogger("snapshot")

# stream2 — лёгкий поток, его достаточно для стоп-кадра
STREAM = "stream2"


def _cfg():
    return json.loads(BOT_CONFIG.read_text(encoding="utf-8"))


def list_cameras():
    """{'1': {'ip':..., 'name':...}, ...}"""
    return _cfg().get("cameras", {})


def pending_count():
    return len(_cfg().get("cameras_pending", {}).get("ips", []))


def grab(cam_id):
    """Возвращает (jpeg_bytes, подпись) или (None, текст ошибки)."""
    cfg = _cfg()
    cams = cfg.get("cameras", {})
    cam = cams.get(str(cam_id))
    if not cam:
        return None, "Камера {} не найдена.".format(cam_id)

    url = "rtsp://{}:{}@{}:554/{}".format(
        cfg["camera_user"], cfg["camera_pass"], cam["ip"], STREAM
    )

    container = None
    try:
        container = av.open(
            url,
            options={"rtsp_transport": "tcp", "stimeout": "8000000"},
            timeout=12,
        )
        for frame in container.decode(video=0):
            buf = io.BytesIO()
            frame.to_image().save(buf, format="JPEG", quality=85)
            return buf.getvalue(), "{} ({})".format(cam["name"], cam["ip"])
        return None, "Поток открылся, но кадр не пришёл."
    except Exception as e:
        log.warning("snapshot %s: %s", cam.get("ip"), e)
        return None, "Не удалось снять кадр с {}: {}".format(cam["name"], e)
    finally:
        if container is not None:
            try:
                container.close()
            except Exception:
                pass


def grab_to_file(cam_id, path):
    data, caption = grab(cam_id)
    if data is None:
        return False, caption
    Path(path).write_bytes(data)
    return True, caption
