"""Отказоустойчивый HTTP-клиент для Telegram-бота HomeGate.

Проблема, которую решает: бот ходит к api.telegram.org, и если текущий
путь наружу отваливается (провайдер режет IP, узел лёг), бот замолкает
без внятной ошибки.

ВАЖНО про формат эндпоинтов: это НЕ сетевые SOCKS5/HTTP-прокси (как
в первой версии модуля), а зеркала Telegram Bot API — запрос идёт
напрямую на другой хост вместо api.telegram.org, тем же путём
/bot<TOKEN>/<METHOD>. Пример: https://3.prilutsky.ru:8443/bot123:ABC/getMe
вместо https://api.telegram.org/bot123:ABC/getMe. Модуль подставляет
базовый URL целиком, а не туннелирует через прокси.

Список ENDPOINTS заполняется владельцем (или в bot.json, ключ
"proxies" — список строк-URL, без хвоста /bot<token>). Модуль ничего
не знает про конкретные адреса — только про механику отработки отказа.

Автор: Клод Антона, 11 августа 2026 — адаптировано под формат
зеркал Telegram Bot API (не SOCKS5) 11 августа 2026.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("homegate.bot.proxy")

# ── Конфиг: заполнить владельцу (или через bot.json -> "proxies") ──
# Порядок = приоритет: сначала пробуется верхний. Последним стоит
# оставить https://api.telegram.org как резерв на случай, если он
# всё-таки отвечает (например, локальный workaround через /etc/hosts).
ENDPOINTS: list[str] = [
    # "https://your-mirror.example.com",
    "https://api.telegram.org",
]

HEALTH_TIMEOUT = 8.0
REQUEST_TIMEOUT = 30.0
COOLDOWN = 120.0
MAX_ATTEMPTS_PER_CALL = None


@dataclass
class _Endpoint:
    """Один выходной путь: базовый URL + его текущее здоровье."""

    base_url: str
    label: str
    dead_until: float = 0.0
    fails: int = 0
    client: httpx.AsyncClient | None = field(default=None, repr=False)

    @property
    def alive(self) -> bool:
        return time.monotonic() >= self.dead_until

    def mark_dead(self) -> None:
        self.fails += 1
        backoff = min(COOLDOWN * (2 ** (self.fails - 1)), 1800.0)
        self.dead_until = time.monotonic() + backoff
        logger.warning("узел %s помечен мёртвым на %.0fс (сбоев подряд: %d)",
                       self.label, backoff, self.fails)

    def mark_alive(self) -> None:
        if self.fails:
            logger.info("узел %s снова жив", self.label)
        self.fails = 0
        self.dead_until = 0.0


class FailoverClient:
    """Telegram Bot API клиент с пулом зеркал и бесшовным переключением.

    Использование:
        fc = FailoverClient(ENDPOINTS, token=BOT_TOKEN)
        await fc.startup()
        r = await fc.call("getMe")
        r = await fc.call("sendMessage", json={"chat_id": ..., "text": ...})
        r = await fc.call("sendPhoto", data={...}, files={...})
        ...
        await fc.aclose()
    """

    def __init__(self, endpoints: list[str] | None, token: str):
        raw = endpoints if endpoints else list(ENDPOINTS)
        self.token = token
        self.endpoints: list[_Endpoint] = [
            _Endpoint(base_url=u.rstrip("/"), label=self._label(u, i))
            for i, u in enumerate(raw)
        ]
        self._lock = asyncio.Lock()

    @staticmethod
    def _label(url: str, idx: int) -> str:
        host = url.split("://")[-1]
        return f"#{idx}:{host}"

    async def startup(self) -> None:
        for ep in self.endpoints:
            ep.client = httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT,
                trust_env=False,
            )
        await self.health_check_all()

    async def health_check_all(self) -> None:
        await asyncio.gather(*(self._probe(ep) for ep in self.endpoints))
        live = [e.label for e in self.endpoints if e.alive]
        logger.info("health-check: живых узлов %d из %d %s",
                    len(live), len(self.endpoints), live)

    async def _probe(self, ep: _Endpoint) -> None:
        try:
            url = f"{ep.base_url}/bot{self.token}/getMe"
            r = await ep.client.get(url, timeout=HEALTH_TIMEOUT)
            if r.status_code < 500:
                ep.mark_alive()
                return
            raise httpx.HTTPError(f"HTTP {r.status_code}")
        except Exception as e:
            logger.debug("health-check %s не прошёл: %s", ep.label, e)
            ep.mark_dead()

    def _ordered(self) -> list[_Endpoint]:
        alive = [e for e in self.endpoints if e.alive]
        dead = [e for e in self.endpoints if not e.alive]
        return alive + dead

    async def call(self, method: str, **kwargs) -> httpx.Response:
        """Вызвать метод Telegram Bot API, перебирая зеркала при сбое.

        method — имя метода (getMe, sendMessage, ...), без /bot<token>/.
        kwargs пробрасываются в httpx (json=, data=, files=, params=, timeout=).
        Бросает httpx.HTTPError только если ВСЕ узлы не сработали.
        """
        candidates = self._ordered()
        limit = MAX_ATTEMPTS_PER_CALL or len(candidates)
        last_err: Exception | None = None

        for ep in candidates[:limit]:
            url = f"{ep.base_url}/bot{self.token}/{method}"
            try:
                r = await ep.client.post(url, **kwargs) if (
                    "json" in kwargs or "data" in kwargs or "files" in kwargs
                ) else await ep.client.get(url, **kwargs)
                if r.status_code >= 500:
                    last_err = httpx.HTTPError(f"{ep.label}: HTTP {r.status_code}")
                    continue
                ep.mark_alive()
                return r
            except (httpx.ConnectError, httpx.ConnectTimeout,
                    httpx.ReadTimeout, httpx.ProxyError,
                    httpx.RemoteProtocolError) as e:
                last_err = e
                ep.mark_dead()
                logger.warning("узел %s не сработал (%s), пробую следующий",
                               ep.label, type(e).__name__)
                continue

        raise httpx.HTTPError(
            f"все {len(candidates)} узлов недоступны; последняя ошибка: {last_err}"
        )

    async def aclose(self) -> None:
        for ep in self.endpoints:
            if ep.client:
                await ep.client.aclose()


def load_endpoints_from_config(path: str = "/opt/homegate/config/bot.json") -> list[str] | None:
    """Читает ключ "proxies" из bot.json (список URL-строк). Отсутствует — None
    (тогда используется дефолтный ENDPOINTS из этого модуля)."""
    import json

    try:
        cfg = json.load(open(path, encoding="utf-8"))
        endpoints = cfg.get("proxies")
        if isinstance(endpoints, list) and endpoints:
            return endpoints
    except Exception as e:
        logger.warning("не удалось прочитать proxies из %s: %s", path, e)
    return None


async def periodic_health(fc: FailoverClient, every: float = 60.0) -> None:
    """Запускать как background task: раз в minute перепроверяет мёртвые узлы."""
    while True:
        await asyncio.sleep(every)
        dead = [e for e in fc.endpoints if not e.alive]
        if dead:
            await asyncio.gather(*(fc._probe(e) for e in dead))
