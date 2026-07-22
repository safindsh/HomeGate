#!/usr/bin/env python3.11
"""
HomeGate MCP — гейт домашнего сервера.

Архитектура (обсуждено с Тигрой, 2026-07-21):
  * Роли разделены токенами:
      - user  (Дима, хозяин дома) — память + чтение состояния дома
      - admin (Тигра)             — всё вышеперечисленное + shell
  * Инструменты типизированные, а не голый shell:
    цена ошибки дома = отопление/свет, а не упавший сайт.
  * Изменение состояния дома — только по белому списку сущностей.
    Отопление/вода/замки/щит в белый список не входят.
  * Qdrant слушает только 127.0.0.1, наружу не проксируется.
"""

import json
import logging
import os
import subprocess
import time
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

# ─────────────────────────── конфиг ───────────────────────────

CONFIG_PATH = os.getenv("HOMEGATE_CONFIG", "/opt/homegate/config/config.json")

with open(CONFIG_PATH) as f:
    CFG = json.load(f)

QDRANT_URL = CFG["qdrant"]["url"]
QDRANT_KEY = CFG["qdrant"]["api_key"]
QDRANT_COLLECTION = CFG["qdrant"]["collection"]

HA_URL = CFG.get("homeassistant", {}).get("url", "")
HA_TOKEN = CFG.get("homeassistant", {}).get("token", "")
HA_ENABLED = bool(HA_URL and HA_TOKEN)

# Белый список сущностей, которыми РАЗРЕШЕНО управлять.
# Пустой = управление запрещено полностью (текущее состояние: HA ещё нет).
HA_WRITE_WHITELIST: set[str] = set(
    CFG.get("homeassistant", {}).get("write_whitelist", [])
)
# Домены, которые НИКОГДА не попадают в управление, даже если внесены в whitelist.
HA_FORBIDDEN_DOMAINS = {"lock", "water_heater", "climate", "valve", "alarm_control_panel"}

TOKENS: dict[str, dict[str, Any]] = CFG["tokens"]  # token -> {"role":..., "name":...}

SHELL_TIMEOUT = CFG.get("shell", {}).get("timeout", 120)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("/opt/homegate/logs/homegate.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("homegate")
audit = logging.getLogger("homegate.audit")
_ah = logging.FileHandler("/opt/homegate/logs/audit.log")
_ah.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
audit.addHandler(_ah)
audit.setLevel(logging.INFO)

app = FastAPI(title="HomeGate MCP", version="1.0.0")


# ─────────────────────────── авторизация ───────────────────────────

def identify(authorization: str | None) -> dict[str, Any]:
    """Достаём личность по Bearer-токену."""
    if not authorization:
        raise HTTPException(401, "missing authorization")
    token = authorization.removeprefix("Bearer ").strip()
    ident = TOKENS.get(token)
    if not ident:
        raise HTTPException(403, "invalid token")
    return ident


def require_admin(ident: dict[str, Any]) -> None:
    if ident.get("role") != "admin":
        raise HTTPException(
            403,
            "этот инструмент доступен только администратору (Тигра)",
        )


# ─────────────────────────── Qdrant: память ───────────────────────────

async def _qdrant(method: str, path: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(
            method,
            f"{QDRANT_URL}{path}",
            headers={"api-key": QDRANT_KEY, "Content-Type": "application/json"},
            json=payload,
        )
        r.raise_for_status()
        return r.json()


_MODEL = None


def _get_model():
    """Ленивая загрузка модели: старт гейта не должен ждать 15 секунд."""
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer

        log.info("загружаю модель эмбеддингов...")
        _MODEL = SentenceTransformer(
            CFG.get("embeddings", {}).get("model", "intfloat/multilingual-e5-small"),
            cache_folder=CFG.get("embeddings", {}).get(
                "cache_folder", "/opt/homegate/models"
            ),
        )
        log.info(
            "модель загружена, размерность %s",
            _MODEL.get_sentence_embedding_dimension(),
        )
    return _MODEL


def _embed(text: str, is_query: bool = False) -> list[float]:
    """
    Векторизация текста моделью multilingual-e5-small (384 измерения).

    E5 требует префиксов: query: для поискового запроса и passage: для
    сохраняемого текста. Без них качество заметно падает — особенность
    обучения модели.
    """
    prefix = "query: " if is_query else "passage: "
    vec = _get_model().encode(prefix + text, normalize_embeddings=True)
    return vec.tolist()


async def memory_save(text: str, tags: list[str] | None, ident: dict) -> str:
    point_id = str(uuid.uuid4())
    await _qdrant(
        "PUT",
        f"/collections/{QDRANT_COLLECTION}/points",
        {
            "points": [
                {
                    "id": point_id,
                    "vector": _embed(text),
                    "payload": {
                        "text": text,
                        "tags": tags or [],
                        "author": ident.get("name", "unknown"),
                        "ts": int(time.time()),
                    },
                }
            ]
        },
    )
    return f"Сохранено в память дома (id={point_id})"


async def memory_search(query: str, limit: int = 5) -> str:
    res = await _qdrant(
        "POST",
        f"/collections/{QDRANT_COLLECTION}/points/search",
        {"vector": _embed(query, is_query=True), "limit": limit, "with_payload": True},
    )
    hits = res.get("result", [])
    if not hits:
        return "Ничего не найдено."
    out = []
    for h in hits:
        p = h.get("payload", {})
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.get("ts", 0)))
        out.append(f"[{ts}] {p.get('text','')} (score={h.get('score',0):.3f})")
    return "\n".join(out)


async def memory_list(limit: int = 20) -> str:
    res = await _qdrant(
        "POST",
        f"/collections/{QDRANT_COLLECTION}/points/scroll",
        {"limit": limit, "with_payload": True},
    )
    pts = res.get("result", {}).get("points", [])
    if not pts:
        return "Память пуста."
    out = []
    for p in pts:
        pl = p.get("payload", {})
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(pl.get("ts", 0)))
        out.append(f"[{ts}] {pl.get('text','')}")
    return "\n".join(out)


# ─────────────────────────── Home Assistant ───────────────────────────

async def _ha(method: str, path: str, payload: dict | None = None) -> Any:
    if not HA_ENABLED:
        raise HTTPException(503, "Home Assistant ещё не подключён (см. config.json)")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(
            method,
            f"{HA_URL}{path}",
            headers={
                "Authorization": f"Bearer {HA_TOKEN}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        r.raise_for_status()
        return r.json()


async def home_state(area: str | None = None) -> str:
    states = await _ha("GET", "/api/states")
    rows = []
    for s in states:
        eid = s["entity_id"]
        if area and area.lower() not in eid.lower():
            continue
        attrs = s.get("attributes", {})
        name = attrs.get("friendly_name", eid)
        unit = attrs.get("unit_of_measurement", "")
        rows.append(f"{name} [{eid}]: {s['state']}{unit}")
    return "\n".join(rows[:200]) or "Сущностей не найдено."


async def sensor_history(entity_id: str, hours: int = 24) -> str:
    import datetime

    start = (
        datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
    ).isoformat() + "Z"
    data = await _ha("GET", f"/api/history/period/{start}?filter_entity_id={entity_id}")
    if not data or not data[0]:
        return f"Нет истории по {entity_id} за {hours}ч."
    pts = data[0]
    out = [f"{p['last_changed'][:16]} → {p['state']}" for p in pts[-50:]]
    return f"История {entity_id} ({len(pts)} точек):\n" + "\n".join(out)


async def home_anomalies() -> str:
    """Не «покажи датчики», а «что выглядит не так».

    Служебные сущности самого HA (бэкапы, TTS, person, погода) к дому
    отношения не имеют и только зашумляют выдачу — отфильтровываем.
    Недоступные группируем по устройству: одно offline-устройство даёт
    4-6 сущностей, перечислять каждую бессмысленно.
    """
    IGNORED_DOMAINS = {
        "conversation", "tts", "stt", "person", "zone", "sun", "backup",
        "event", "update", "todo", "script", "automation", "scene",
        "input_boolean", "input_number", "input_text", "input_select",
        "device_tracker", "assist_satellite", "ai_task", "weather",
    }
    IGNORED_PREFIXES = ("sensor.backup_", "sensor.sun_")
    ATTR_SUFFIXES = (
        " Ток", " Мощность", " Напряжение", " Всего энергия",
        " Поведение при запуске", " Режим светового индикатора",
        " Блокировка от детей", " Socket", " Door", " Состояние батареи",
    )

    states = await _ha("GET", "/api/states")
    now = time.time()

    offline: dict[str, int] = {}
    problems: list[str] = []

    for s in states:
        eid = s["entity_id"]
        domain = eid.split(".")[0]
        if domain in IGNORED_DOMAINS or eid.startswith(IGNORED_PREFIXES):
            continue

        st = s["state"]
        attrs = s.get("attributes", {})
        name = attrs.get("friendly_name", eid)

        if st in ("unavailable", "unknown"):
            device = name
            for suf in ATTR_SUFFIXES:
                device = device.split(suf)[0]
            offline[device] = offline.get(device, 0) + 1
            continue

        try:
            lc = s.get("last_changed", "")
            import datetime

            t = datetime.datetime.fromisoformat(lc.replace("Z", "+00:00")).timestamp()
            if eid.startswith("sensor.") and now - t > 86400:
                problems.append(f"МОЛЧИТ >24ч: {name} [{eid}], последнее значение {st}")
        except Exception:
            pass

        if attrs.get("device_class") == "battery":
            try:
                if float(st) < 20:
                    problems.append(f"БАТАРЕЯ {st}%: {name} [{eid}]")
            except ValueError:
                pass

        if eid.endswith("_battery_state") and st in ("low", "empty"):
            problems.append(f"БАТАРЕЯ {st}: {name} [{eid}]")

    out: list[str] = []
    if offline:
        out.append("НЕ НА СВЯЗИ:")
        for device, cnt in sorted(offline.items()):
            out.append(f"  - {device} ({cnt})")
    if problems:
        if out:
            out.append("")
        out.extend(problems)

    return "\n".join(out) if out else "Аномалий не обнаружено."


async def device_control(entity_id: str, action: str, ident: dict) -> str:
    domain = entity_id.split(".")[0]
    if domain in HA_FORBIDDEN_DOMAINS:
        return (
            f"ОТКАЗ: домен '{domain}' запрещён для автоматического управления "
            f"(отопление/вода/замки/щит). Требуется ручное действие хозяина."
        )
    if entity_id not in HA_WRITE_WHITELIST:
        return (
            f"ОТКАЗ: {entity_id} не в белом списке разрешённых для управления.\n"
            f"Разрешено сейчас: {sorted(HA_WRITE_WHITELIST) or '(список пуст)'}\n"
            f"Добавить: /opt/homegate/config/config.json → homeassistant.write_whitelist"
        )
    if action not in ("turn_on", "turn_off", "toggle"):
        return f"ОТКАЗ: недопустимое действие '{action}'."

    await _ha("POST", f"/api/services/{domain}/{action}", {"entity_id": entity_id})
    audit.info(f"DEVICE_CONTROL user={ident.get('name')} {entity_id} {action}")
    return f"Выполнено: {entity_id} → {action}"


# ─────────────────────────── shell (admin only) ───────────────────────────

def run_command(command: str, ident: dict) -> str:
    audit.info(f"SHELL user={ident.get('name')} cmd={command!r}")
    try:
        p = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT,
        )
        out = ""
        if p.stdout:
            out += f"STDOUT:\n{p.stdout}"
        if p.stderr:
            out += f"\nSTDERR:\n{p.stderr}"
        if p.returncode != 0:
            out += f"\nEXIT: {p.returncode}"
        return out or "(пустой вывод)"
    except subprocess.TimeoutExpired:
        return f"Команда превысила таймаут {SHELL_TIMEOUT}с."


# ─────────────────────────── описание инструментов ───────────────────────────

TOOLS_USER = [
    {
        "name": "memory_save",
        "description": "Сохранить факт или контекст в память дома.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Что запомнить"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["text"],
        },
    },
    {
        "name": "memory_search",
        "description": "Поиск по памяти дома.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_list",
        "description": "Последние записи в памяти дома.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    },
    {
        "name": "home_state",
        "description": "Текущее состояние дома: датчики, температуры, автоматика.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "area": {"type": "string", "description": "Фильтр по зоне/подстроке"}
            },
        },
    },
    {
        "name": "sensor_history",
        "description": "История значений датчика за период.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "hours": {"type": "integer", "default": 24},
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "home_anomalies",
        "description": "Что в доме выглядит не так: молчащие датчики, севшие батареи, недоступные устройства.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "device_control",
        "description": "Управление устройством из белого списка (turn_on/turn_off/toggle).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "action": {"type": "string", "enum": ["turn_on", "turn_off", "toggle"]},
            },
            "required": ["entity_id", "action"],
        },
    },
]

TOOLS_ADMIN = TOOLS_USER + [
    {
        "name": "run_command",
        "description": "Выполнить shell-команду на сервере. Только для администратора.",
        "inputSchema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "service_status",
        "description": "Статус ключевых сервисов: docker, nginx, qdrant, node_exporter.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def service_status() -> str:
    out = []
    for unit in ("nginx", "docker", "node_exporter"):
        r = subprocess.run(
            ["systemctl", "is-active", unit], capture_output=True, text=True
        )
        out.append(f"{unit}: {r.stdout.strip()}")
    r = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}} {{.Status}}"],
        capture_output=True,
        text=True,
    )
    out.append("--- контейнеры ---")
    out.append(r.stdout.strip() or "(нет)")
    return "\n".join(out)


# ─────────────────────────── MCP endpoint ───────────────────────────

@app.post("/claude-mcp")
async def mcp(request: Request, authorization: str | None = Header(None)):
    ident = identify(authorization)
    body = await request.json()
    method = body.get("method")
    rpc_id = body.get("id")
    params = body.get("params", {})

    def ok(result: Any) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    if method == "initialize":
        return ok(
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "homegate", "version": "1.0.0"},
            }
        )

    if method == "notifications/initialized":
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {}})

    if method == "tools/list":
        tools = TOOLS_ADMIN if ident.get("role") == "admin" else TOOLS_USER
        return ok({"tools": tools})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        log.info("call %s by %s", name, ident.get("name"))

        try:
            if name == "memory_save":
                text = await memory_save(args["text"], args.get("tags"), ident)
            elif name == "memory_search":
                text = await memory_search(args["query"], args.get("limit", 5))
            elif name == "memory_list":
                text = await memory_list(args.get("limit", 20))
            elif name == "home_state":
                text = await home_state(args.get("area"))
            elif name == "sensor_history":
                text = await sensor_history(args["entity_id"], args.get("hours", 24))
            elif name == "home_anomalies":
                text = await home_anomalies()
            elif name == "device_control":
                text = await device_control(args["entity_id"], args["action"], ident)
            elif name == "run_command":
                require_admin(ident)
                text = run_command(args["command"], ident)
            elif name == "service_status":
                require_admin(ident)
                text = service_status()
            else:
                raise HTTPException(400, f"неизвестный инструмент: {name}")
        except HTTPException as e:
            text = f"Ошибка: {e.detail}"
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s failed", name)
            text = f"Ошибка выполнения: {e}"

        return ok({"content": [{"type": "text", "text": text}]})

    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    )


@app.get("/claude-mcp/health")
async def health():
    return {"status": "ok", "ha_enabled": HA_ENABLED, "ts": int(time.time())}
