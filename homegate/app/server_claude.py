"""HomeGate как FastMCP-сервер — для подключения коннектором в Claude.

Форма коннектора Claude умеет регистрировать только FastMCP-совместимые
серверы (как 5.prilutsky.ru и SkyNet). Ручной MCP на FastAPI в homegate.py
она не принимает. Поэтому здесь тонкая FastMCP-обёртка, переиспользующая
уже написанную и отлаженную логику дома из homegate.py.

Доступ режется allow-спиком nginx (только подсеть агентов Claude), поэтому
внутри считаем вызовы доверенными и работаем под admin-личностью — так же,
как это делают остальные коннекторы Тигры.
"""

import asyncio
import logging
import sys

from fastmcp import FastMCP

sys.path.insert(0, "/opt/homegate/app")
import homegate as hg  # noqa: E402  — переиспуем всю логику дома

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("homegate-mcp")

# Доступ уже ограничен nginx (allow-список подсети агентов). Внутри —
# фиксированная admin-личность, как трактует доверенный запрос основной гейт.
AGENT = {"name": "claude-agent", "role": "admin"}

mcp = FastMCP("homegate")


@mcp.tool()
async def ping() -> str:
    """Простой пинг — проверка, что коннектор жив."""
    return "pong"


@mcp.tool()
async def home_state(area: str | None = None) -> str:
    """Состояние дома: устройства, датчики, их значения. area — фильтр по зоне."""
    return await hg.home_state(area)


@mcp.tool()
async def home_anomalies() -> str:
    """Что требует внимания: устройства не на связи и садящиеся батареи."""
    return await hg.home_anomalies()


@mcp.tool()
async def sensor_history(entity_id: str, hours: int = 24) -> str:
    """История значений одной сущности за период (по умолчанию сутки)."""
    return await hg.sensor_history(entity_id, hours)


@mcp.tool()
async def device_control(entity_id: str, action: str, dry_run: bool = True) -> str:
    """Управление устройством из белого списка (turn_on/turn_off/toggle).

    По умолчанию СУХОЙ ПРОГОН: показывает, что изменится, но не выполняет.
    Для реального действия передать dry_run=false.
    """
    return await hg.device_control(entity_id, action, AGENT, dry_run)


@mcp.tool()
async def run_command(command: str) -> str:
    """Выполнить команду в шелле сервера (root). Таймаут из конфига."""
    return hg.run_command(command, AGENT)


@mcp.tool()
async def service_status() -> str:
    """Статус ключевых сервисов и контейнеров дома."""
    return hg.service_status()


@mcp.tool()
async def memory_search(query: str, limit: int = 5) -> str:
    """Поиск по векторной памяти дома (что где стоит, решения, что не трогать)."""
    return await hg.memory_search(query, limit)


@mcp.tool()
async def memory_save(text: str, tags: str = "") -> str:
    """Сохранить факт в векторную память дома."""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    return await hg.memory_save(text, tag_list, AGENT)


import time as _time


@mcp.tool()
async def chat_save(text: str, tags: str = "") -> str:
    """Сохранить фрагмент нашего разговора с Claude в память дома.

    Пишет в ту же коллекцию dima_memory, но с меткой claude_chat и
    автором claude — чтобы наши диалоги были отделимы от фактов о доме.
    tags — доп. теги через запятую (метка claude_chat добавляется всегда).
    """
    extra = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    tag_list = ["claude_chat"] + extra
    return await hg.memory_save(text, tag_list, {"name": "claude"})


@mcp.tool()
async def chat_search(query: str, limit: int = 5, only_chats: bool = False) -> str:
    """Поиск по памяти. only_chats=true — искать только по нашим разговорам
    (метка claude_chat); иначе по всей памяти дома вместе с чатами."""
    body = {
        "vector": hg._embed(query, is_query=True),
        "limit": limit,
        "with_payload": True,
    }
    if only_chats:
        body["filter"] = {"must": [{"key": "tags", "match": {"value": "claude_chat"}}]}
    res = await hg._qdrant(
        "POST", f"/collections/{hg.QDRANT_COLLECTION}/points/search", body
    )
    hits = res.get("result", [])
    if not hits:
        return "Ничего не найдено."
    lines = []
    for h in hits:
        p = h.get("payload", {})
        ts = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(p.get("ts", 0)))
        who = p.get("author", "?")
        txt = p.get("text", "")
        score = h.get("score", 0)
        lines.append("[{}] ({}) {} (score={:.3f})".format(ts, who, txt, score))
    return "\n".join(lines)


if __name__ == "__main__":
    logger.info("Starting HomeGate FastMCP (streamable HTTP, port 8801)")
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8801)
