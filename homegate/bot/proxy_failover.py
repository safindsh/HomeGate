"""Отказоустойчивый HTTP-клиент для Telegram-бота HomeGate.

Проблема, которую решает: бот ходит к api.telegram.org, и если текущий
путь наружу отваливается (провайдер режет IP, узел лёг), бот замолкает
без внятной ошибки. Здесь — пул из нескольких выходных путей: клиент
сам проверяет их живость, ходит через рабочий и бесшовно переключается
на следующий при сбое, а упавший периодически пробует вернуть.

Список PROXIES заполняется владельцем (или в bot.json). Модуль ничего
не знает про конкретные адреса — только про механику отработки отказа.

Зависимости: httpx[socks]  (pip install "httpx[socks]")
Формат прокси-строки httpx: socks5://user:pass@host:port  либо
http://host:port  либо None (прямое соединение, без прокси).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("homegate.bot.proxy")

# ── Конфиг: заполнить владельцу ───────────────────────────────────
# Три слота. Порядок = приоритет: сначала пробуется верхний.
# None означает "прямое соединение без прокси" — можно оставить один из вариантов, если прямой путь иногда работает.
# Реальные адреса НЕ вписаны намеренно — подставить здесь или прокинуть
# из bot.json (см. load_proxies_from_config ниже).
PROXIES: list[str | None] = [
    # "socks5://user:pass@HOST_A:PORT",
    # "socks5://user:pass@HOST_B:PORT",
    # "socks5://user:pass@HOST_C:PORT",
]

# Куда стучимся для проверки живости пути. getMe — дёшево и без побочек.
HEALTH_URL = "https://api.telegram.org"
HEALTH_TIMEOUT = 8.0        # сек на health-check одного узла
REQUEST_TIMEOUT = 30.0      # сек на обычный запрос (long polling задаёт свой)
COOLDOWN = 120.0            # сколько держать узел "мёртвым" после сбоя, сек
MAX_ATTEMPTS_PER_CALL = None  # None = перебрать все живые узлы за один вызов


@dataclass
class _Endpoint:
    """Один выходной путь: прокси-строка + его текущее здоровье."""

    proxy: str | None
    label: str
    dead_until: float = 0.0          # timestamp, до которого считаем мёртвым
    fails: int = 0                   # подряд неудач (для экспоненты cooldown)
    client: httpx.AsyncClient | None = field(default=None, repr=False)

    @property
    def alive(self) -> bool:
        return time.monotonic() >= self.dead_until

    def mark_dead(self) -> None:
        self.fails += 1
        # экспоненциальный, но с потолком: 120с, 240с, 480с, ... до 30 мин
        backoff = min(COOLDOWN * (2 ** (self.fails - 1)), 1800.0)
        self.dead_until = time.monotonic() + backoff
        logger.warning("прокси %s помечен мёртвым на %.0fс (сбоев подряд: %d)",
                       self.label, backoff, self.fails)

    def mark_alive(self) -> None:
        if self.fails:
            logger.info("прокси %s снова жив", self.label)
        self.fails = 0
        self.dead_until = 0.0


class FailoverClient:
    """HTTP-клиент с пулом выходных путей и бесшовным переключением.

    Использование:
        fc = FailoverClient(PROXIES)
        await fc.startup()          # поднять клиентов, проверить узлы
        r = await fc.request("GET", "https://api.telegram.org/bot.../getMe")
        ...
        await fc.aclose()
    """

    def __init__(self, proxies: list[str | None] | None = None):
        raw = proxies if proxies is not None else PROXIES
        if not raw:
            # пустой пул = один прямой путь, чтобы бот не падал на старте
            raw = [None]
        self.endpoints: list[_Endpoint] = [
            _Endpoint(proxy=p, label=self._label(p, i)) for i, p in enumerate(raw)
        ]
        self._lock = asyncio.Lock()

    @staticmethod
    def _label(proxy: str | None, idx: int) -> str:
        if proxy is None:
            return f"#{idx}:direct"
        # прячем креды в логах: socks5://user:pass@host:port -> host:port
        tail = proxy.split("@")[-1]
        scheme = proxy.split("://")[0]
        return f"#{idx}:{scheme}://{tail}"

    async def startup(self) -> None:
        """Поднять по клиенту на каждый путь и прогнать health-check."""
        for ep in self.endpoints:
            ep.client = httpx.AsyncClient(
                proxy=ep.proxy,
                timeout=REQUEST_TIMEOUT,
                trust_env=False,   # не подхватывать системные HTTP(S)_PROXY
            )
        await self.health_check_all()

    async def health_check_all(self) -> None:
        """Параллельно проверить живость всех узлов."""
        await asyncio.gather(*(self._probe(ep) for ep in self.endpoints))
        live = [e.label for e in self.endpoints if e.alive]
        logger.info("health-check: живых путей %d из %d %s",
                    len(live), len(self.endpoints), live)

    async def _probe(self, ep: _Endpoint) -> None:
        try:
            r = await ep.client.get(HEALTH_URL, timeout=HEALTH_TIMEOUT)
            # api.telegram.org на корень отдаёт 404 — это ОК, путь живой
            if r.status_code < 500:
                ep.mark_alive()
                return
            raise httpx.HTTPError(f"HTTP {r.status_code}")
        except Exception as e:
            logger.debug("health-check %s не прошёл: %s", ep.label, e)
            ep.mark_dead()

    def _ordered(self) -> list[_Endpoint]:
        """Живые узлы в порядке приоритета, затем — мёртвые как последний шанс."""
        alive = [e for e in self.endpoints if e.alive]
        dead = [e for e in self.endpoints if not e.alive]
        return alive + dead

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Выполнить запрос, перебирая пути при сбое.

        Бросает httpx.HTTPError только если ВСЕ пути не сработали.
        """
        candidates = self._ordered()
        limit = MAX_ATTEMPTS_PER_CALL or len(candidates)
        last_err: Exception | None = None

        for ep in candidates[:limit]:
            try:
                r = await ep.client.request(method, url, **kwargs)
                # 5xx от Telegram — не вина пути, не караем прокси, но пробуем
                # следующий, вдруг это узловой сбой. 4xx/2xx = путь рабочий.
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
                logger.warning("путь %s не сработал (%s), пробую следующий",
                               ep.label, type(e).__name__)
                continue

        raise httpx.HTTPError(
            f"все {len(candidates)} путей недоступны; последняя ошибка: {last_err}"
        )

    async def aclose(self) -> None:
        for ep in self.endpoints:
            if ep.client:
                await ep.client.aclose()


# ── Помощник: подтянуть прокси из bot.json, не хардкодя в коде ─────
def load_proxies_from_config(path: str = "/opt/homegate/config/bot.json") -> list[str | None]:
    """Читает ключ "proxies" из bot.json (список строк). Отсутствует — [None].

    Пример секции в bot.json:
        "proxies": [
            "socks5://user:pass@host_a:1080",
            "socks5://user:pass@host_b:1080",
            null
        ]
    null в списке = прямое соединение как один из вариантов.
    """
    import json

    try:
        cfg = json.load(open(path, encoding="utf-8"))
        proxies = cfg.get("proxies")
        if isinstance(proxies, list) and proxies:
            return proxies
    except Exception as e:
        logger.warning("не удалось прочитать proxies из %s: %s", path, e)
    return [None]


# ── Опциональный фоновый ре-хелсчек: оживляет упавшие узлы ─────────
async def periodic_health(fc: FailoverClient, every: float = 60.0) -> None:
    """Запускать как background task: раз в minute перепроверяет мёртвые узлы."""
    while True:
        await asyncio.sleep(every)
        dead = [e for e in fc.endpoints if not e.alive]
        if dead:
            await asyncio.gather(*(fc._probe(e) for e in dead))
