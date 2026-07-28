#!/usr/bin/env python3
"""
HomeGate Telegram Bot
Читает состояние дома, шлёт алармы, управляет устройствами из белого списка.

Безопасность (наследует принципы гейта):
  - отвечает ТОЛЬКО владельцу (chat_id), остальным молчит;
  - управление только для сущностей из write_whitelist;
  - домены lock/climate/water_heater/valve/alarm_control_panel запрещены в коде;
  - каждая команда управления пишется в аудит-лог.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

CONFIG_PATH = Path("/opt/homegate/config/config.json")
BOT_CONFIG_PATH = Path("/opt/homegate/config/bot.json")
AUDIT_LOG = Path("/opt/homegate/logs/bot_audit.log")

FORBIDDEN_DOMAINS = {
    "lock",
    "climate",
    "water_heater",
    "valve",
    "alarm_control_panel",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("tgbot")


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    bot_cfg = json.loads(BOT_CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        "ha_url": cfg["homeassistant"]["url"].rstrip("/"),
        "ha_token": cfg["homeassistant"]["token"],
        "bot_token": bot_cfg["token"],
        "chat_id": int(bot_cfg["chat_id"]),
        "whitelist": bot_cfg.get("write_whitelist", []),
    }


CFG = load_config()
TG_API = "https://api.telegram.org/bot" + CFG["bot_token"]


def audit(chat_id, action: str, detail: str) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = "{}\t{}\t{}\t{}\n".format(
        time.strftime("%Y-%m-%d %H:%M:%S"), chat_id, action, detail
    )
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(line)


async def ha_get(client, path: str):
    r = await client.get(
        CFG["ha_url"] + path,
        headers={"Authorization": "Bearer " + CFG["ha_token"]},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


async def ha_post(client, path: str, payload: dict):
    r = await client.post(
        CFG["ha_url"] + path,
        headers={
            "Authorization": "Bearer " + CFG["ha_token"],
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


async def get_states(client) -> dict:
    data = await ha_get(client, "/api/states")
    return {s["entity_id"]: s for s in data}


def fmt_home(states: dict) -> str:
    def st(eid, default="-"):
        s = states.get(eid)
        return s["state"] if s else default

    smoke = st("binary_sensor.smoke_alarm_smoke")
    gate = st("cover.wifi_garage_door_module_door_1")

    power_keys = [
        "sensor.exclusive_shkaf_power",
        "sensor.wifi_rozetka_power",
        "sensor.maskitol_v_komnate_gleba_power",
        "sensor.maskitol_v_komnate_iana_power",
        "sensor.moskitol_spalnia_power",
    ]
    total = 0.0
    for k in power_keys:
        try:
            total += float(st(k, "0"))
        except ValueError:
            pass

    lines = [
        "<b>Дом сейчас</b>",
        "",
        "Дым: " + ("ТРЕВОГА" if smoke == "on" else "норма"),
        "Ворота: " + ("закрыты" if gate == "closed" else "открыты"),
        "Батарея дымового: " + st("sensor.smoke_alarm_battery_state"),
        "",
        "Суммарная мощность: <b>{:.0f} Вт</b>".format(total),
    ]

    offline = [
        s["attributes"].get("friendly_name", eid)
        for eid, s in states.items()
        if s["state"] == "unavailable"
    ]
    if offline:
        lines += ["", "Не на связи ({}):".format(len(offline))]
        lines += ["- " + n for n in sorted(set(offline))[:10]]

    return "\n".join(lines)


def fmt_energy(states: dict) -> str:
    groups = [
        ("Exclusive шкаф", "exclusive_shkaf"),
        ("WiFi-розетка", "wifi_rozetka"),
        ("Насос бассейна", "nasos_basseina"),
        ("Москитол Глеб", "maskitol_v_komnate_gleba"),
        ("Москитол Ян", "maskitol_v_komnate_iana"),
        ("Москитол спальня", "moskitol_spalnia"),
    ]
    lines = ["<b>Энергия</b>", ""]
    for title, key in groups:
        p = states.get("sensor." + key + "_power")
        v = states.get("sensor." + key + "_voltage")
        t = states.get("sensor." + key + "_total_energy")
        if not p:
            continue
        if p["state"] == "unavailable":
            lines.append(title + ": нет связи")
            continue
        lines.append(
            "{}: <b>{} Вт</b> ({} В, всего {} кВт*ч)".format(
                title,
                p["state"],
                v["state"] if v else "-",
                t["state"] if t else "-",
            )
        )
    return "\n".join(lines)


def fmt_anomalies(states: dict) -> str:
    offline, low_bat = [], []
    for eid, s in states.items():
        name = s["attributes"].get("friendly_name", eid)
        if s["state"] == "unavailable":
            offline.append(name)
        if "battery" in eid and s["state"] not in ("unavailable", "high", "unknown"):
            low_bat.append(name + ": " + s["state"])

    lines = ["<b>Что требует внимания</b>", ""]
    if not offline and not low_bat:
        lines.append("Всё в порядке.")
        return "\n".join(lines)
    if offline:
        lines.append("Не на связи ({}):".format(len(offline)))
        lines += ["- " + n for n in sorted(set(offline))]
    if low_bat:
        lines += ["", "Батареи:"] + ["- " + n for n in low_bat]
    return "\n".join(lines)


def fmt_whitelist() -> str:
    wl = CFG["whitelist"]
    if not wl:
        return (
            "<b>Белый список пуст.</b>\n\n"
            "Бот пока ничем управлять не может - это защита по умолчанию.\n"
            "Чтобы разрешить устройство, впиши его entity_id в "
            "write_whitelist в файле /opt/homegate/config/bot.json "
            "и перезапусти сервис."
        )
    return "<b>Разрешено управлять:</b>\n" + "\n".join("- <code>" + e + "</code>" for e in wl)


async def do_switch(client, chat_id: int, entity: str, turn_on: bool) -> str:
    domain = entity.split(".")[0]

    if domain in FORBIDDEN_DOMAINS:
        audit(chat_id, "DENIED_DOMAIN", entity)
        return "Домен <code>" + domain + "</code> запрещён в коде. Команда не выполнена."

    if entity not in CFG["whitelist"]:
        audit(chat_id, "DENIED_WHITELIST", entity)
        return (
            "<code>" + entity + "</code> не в белом списке.\n"
            "Посмотреть разрешённое: /whitelist"
        )

    service = "turn_on" if turn_on else "turn_off"
    try:
        await ha_post(client, "/api/services/" + domain + "/" + service,
                      {"entity_id": entity})
    except Exception as e:
        audit(chat_id, "ERROR", entity + " " + service + " " + str(e))
        return "Ошибка выполнения: " + str(e)

    audit(chat_id, "EXEC", entity + " " + service)
    return "<code>" + entity + "</code> -> <b>" + ("включено" if turn_on else "выключено") + "</b>"


async def tg_send(client, chat_id: int, text: str):
    try:
        await client.post(
            TG_API + "/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
    except Exception as e:
        log.warning("sendMessage failed: %s", e)


HELP = """<b>HomeGate - бот дома</b>

/дом - сводка: дым, ворота, мощность, кто офлайн
/энергия - потребление по розеткам
/аномалии - что требует внимания
/whitelist - чем разрешено управлять
/вкл entity_id - включить
/выкл entity_id - выключить
/кадр - стоп-кадр с камеры
/help - эта справка

Управление работает только для устройств из белого списка.
Отопление, замки, водонагреватель, вентили и сигнализация
заблокированы на уровне кода."""


async def handle(client, msg: dict):
    chat_id = msg["chat"]["id"]

    if chat_id != CFG["chat_id"]:
        log.warning("сообщение от постороннего chat_id=%s - игнор", chat_id)
        return

    text = (msg.get("text") or "").strip()
    if not text:
        return

    parts = text.split()
    cmd = parts[0].lower().lstrip("/").split("@")[0]
    args = parts[1:]

    if cmd in ("start", "help", "справка"):
        await tg_send(client, chat_id, HELP)
        return

    if cmd in ("дом", "home", "статус"):
        await tg_send(client, chat_id, fmt_home(await get_states(client)))
        return

    if cmd in ("энергия", "energy", "мощность"):
        await tg_send(client, chat_id, fmt_energy(await get_states(client)))
        return

    if cmd in ("аномалии", "anomalies", "проблемы"):
        await tg_send(client, chat_id, fmt_anomalies(await get_states(client)))
        return

    if cmd in ("whitelist", "список"):
        await tg_send(client, chat_id, fmt_whitelist())
        return

    if cmd in ("вкл", "on", "включи"):
        if not args:
            await tg_send(client, chat_id,
                          "Укажи entity_id: /вкл switch.wifi_rozetka_socket_1")
            return
        await tg_send(client, chat_id, await do_switch(client, chat_id, args[0], True))
        return

    if cmd in ("выкл", "off", "выключи"):
        if not args:
            await tg_send(client, chat_id,
                          "Укажи entity_id: /выкл switch.wifi_rozetka_socket_1")
            return
        await tg_send(client, chat_id, await do_switch(client, chat_id, args[0], False))
        return

    if cmd in ("кадр", "снимок", "snapshot", "cam"):
        await tg_send(
            client, chat_id,
            "Камеры пока не подключены.\n\n"
            "Нужно в приложении Tapo для каждой камеры создать "
            "<b>Camera Account</b> (Настройки -> Дополнительно -> "
            "Учётная запись камеры). После этого станет доступен RTSP "
            "и я включу сюда стоп-кадры.",
        )
        return

    await tg_send(client, chat_id, "Не понял команду. /help - список того, что умею.")


async def alarm_watch(client):
    prev = {}
    watch = {
        "binary_sensor.smoke_alarm_smoke": ("ДЫМ! Сработал датчик дыма.", "on"),
        "cover.wifi_garage_door_module_door_1": ("Ворота открыты.", "open"),
    }
    while True:
        try:
            states = await get_states(client)
            for eid, pair in watch.items():
                text, trigger = pair
                s = states.get(eid)
                if not s:
                    continue
                cur = s["state"]
                if eid in prev and prev[eid] != cur and cur == trigger:
                    await tg_send(client, CFG["chat_id"], text)
                    audit(0, "ALERT", eid + "=" + cur)
                prev[eid] = cur
        except Exception as e:
            log.warning("alarm_watch: %s", e)
        await asyncio.sleep(30)


async def main():
    log.info("HomeGate bot стартует, владелец chat_id=%s", CFG["chat_id"])
    async with httpx.AsyncClient() as client:
        try:
            await client.get(TG_API + "/deleteWebhook", timeout=15)
        except Exception:
            pass

        asyncio.create_task(alarm_watch(client))

        offset = None
        while True:
            try:
                r = await client.get(
                    TG_API + "/getUpdates",
                    params={"timeout": 30, "offset": offset},
                    timeout=40,
                )
                data = r.json()
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("edited_message")
                    if msg:
                        try:
                            await handle(client, msg)
                        except Exception as e:
                            log.exception("handle error: %s", e)
            except Exception as e:
                log.warning("polling error: %s", e)
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
