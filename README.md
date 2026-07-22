# HomeGate

Конфигурация домашнего сервера: MCP-гейт, векторная память, мониторинг, веб-доступ.

Сервер стоит в частном доме и обслуживает домашнюю автоматику. **Не связан
с рабочей инфраструктурой** — отдельные токены, отдельная память, отдельные бэкапы.

## Что здесь

```
homegate/app/homegate.py      MCP-гейт: FastAPI, типизированные инструменты
homegate/config/              config.example.json — шаблон, реальный конфиг не в git
qdrant/                       векторная память (compose)
monitoring/                   Prometheus + Grafana + дашборды (compose, provisioning)
homeassistant/                Home Assistant (compose + configuration.yaml)
nginx/homegate.conf           реверс-прокси, TLS
nginx/ha.conf                 Home Assistant на отдельном порту 8443
systemd/                      юниты homegate и node_exporter
docs/README.server.md         эксплуатационная документация машины
```

## Секреты

**В репозитории их нет и быть не должно.** Токены, API-ключи и пароли живут
только на сервере в файлах с `chmod 600`:

| Файл на сервере | Шаблон в репо |
|---|---|
| `/opt/homegate/config/config.json` | `homegate/config/config.example.json` |
| `/opt/monitoring/.env` | `monitoring/.env.example` |
| `/opt/qdrant/.env` | `qdrant/.env.example` |

`.gitignore` блокирует `config.json`, `*.env`, ключи и сертификаты. Если секрет
всё же попал в коммит — недостаточно удалить его следующим коммитом, он остаётся
в истории: нужно переписывать историю и менять сам секрет.

## Архитектура гейта

Инструменты **типизированные**, а не голый shell. Дома цена ошибки выше, чем на
рабочем сервере: там упадёт сайт, здесь останется без отопления живой человек.

- Роли разделены токенами: `user` (хозяин дома) и `admin`
- У роли `user` нет shell — `tools/list` его не отдаёт, прямой вызов отклоняется
- Чтение состояния дома свободно, запись — только по `write_whitelist`
- Домены `lock`, `climate`, `water_heater`, `valve`, `alarm_control_panel`
  запрещены **в коде**: даже если внести их в whitelist, управление откажет
- Каждый shell-вызов и каждое управление устройством пишется в `audit.log`

### Инструменты

| Инструмент | user | admin |
|---|---|---|
| `memory_save` / `memory_search` / `memory_list` | + | + |
| `home_state` / `sensor_history` / `home_anomalies` | + | + |
| `device_control` | + | + |
| `run_command` | — | + |
| `service_status` | — | + |

`home_anomalies` отвечает не «вот все датчики», а «вот что выглядит не так»:
молчащие больше суток сенсоры, батареи ниже 20%, недоступные устройства.

## Развёртывание с нуля

Rocky Linux 9, root.

```bash
# 1. пакеты
dnf install -y epel-release
dnf install -y nginx certbot python3-certbot-nginx python3.11 python3.11-pip \
               git policycoreutils-python-utils
# docker по официальной инструкции

# 2. каталоги
mkdir -p /opt/homegate/{app,config,logs} /opt/qdrant/{storage,snapshots} \
         /opt/monitoring /var/www/dashboards

# 3. файлы из репозитория
cp homegate/app/homegate.py        /opt/homegate/app/
cp qdrant/docker-compose.yml       /opt/qdrant/
cp -r monitoring/*                 /opt/monitoring/
cp nginx/homegate.conf             /etc/nginx/conf.d/
cp systemd/*.service               /etc/systemd/system/

# 4. секреты — сгенерировать свои, не переиспользовать чужие
cp homegate/config/config.example.json /opt/homegate/config/config.json
cp monitoring/.env.example             /opt/monitoring/.env
cp qdrant/.env.example                 /opt/qdrant/.env
chmod 600 /opt/homegate/config/config.json /opt/monitoring/.env /opt/qdrant/.env
# заполнить: openssl rand -hex 32

# 5. SELinux (иначе systemd упадёт с 209/STDOUT)
semanage fcontext -a -t var_log_t "/opt/homegate/logs(/.*)?"
restorecon -Rv /opt/homegate/logs

# 6. python
python3.11 -m venv /opt/homegate/venv
/opt/homegate/venv/bin/pip install fastapi uvicorn httpx

# 7. сертификат (самоподписанный; с доменом — certbot --nginx -d <домен>)
mkdir -p /etc/nginx/ssl
openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
  -keyout /etc/nginx/ssl/selfsigned.key -out /etc/nginx/ssl/selfsigned.crt \
  -subj "/CN=homegate.local"

# 8. запуск
systemctl daemon-reload
systemctl enable --now nginx homegate node_exporter
cd /opt/qdrant     && docker compose up -d
cd /opt/monitoring && docker compose up -d
```

Коллекция памяти создаётся один раз:

```bash
curl -X PUT http://127.0.0.1:6333/collections/dima_memory \
  -H "api-key: $QDRANT_API_KEY" -H "Content-Type: application/json" \
  -d "{\"vectors\":{\"size\":1024,\"distance\":\"Cosine\"}}"
```

## Грабли

- **SELinux Enforcing.** Новые каталоги логов требуют контекста, иначе systemd
  падает с `status=209/STDOUT`. Для bind-mount в Docker — суффикс `:Z`.
- **nginx 1.20**, не 1.25: `listen 443 ssl http2;` одной строкой; отдельной
  директивы `http2 on;` нет.
- **Grafana за префиксом.** Нужны и `GF_SERVER_ROOT_URL`, и
  `GF_SERVER_SERVE_FROM_SUB_PATH=true`, а `proxy_pass` — **без** трейлинг-слеша
  (`http://127.0.0.1:3000`, не с косой чертой на конце). Со слешем nginx срезает
  префикс `/grafana` и получается цикл редиректов.
- **Процент в docker compose.** Значения с плейсхолдерами вида `%(protocol)s`
  compose интерпретирует как подстановку. Проще указать статичный URL.
- Дефолтный `server`-блок вырезан из `nginx.conf`, иначе конфликт по `server_name _`.
- Передача больших файлов через обёртку MCP по кускам base64 — источник ошибок.
  Лучше собирать файл на месте через heredoc и сверять `sha256sum`.

## Состояние

Работает: nginx + TLS, MCP-гейт, Qdrant, Prometheus + Grafana с дашбордами,
Home Assistant (поднят, ждёт первичной настройки владельцем).

### Точки входа

| Адрес | Что |
|---|---|
| `https://<host>/` | дашборды |
| `https://<host>/grafana/` | Grafana |
| `https://<host>/claude-mcp/health` | MCP-гейт |
| `https://<host>:8443/` | Home Assistant |

**Home Assistant не умеет работать в подкаталоге** — у него нет аналога
`serve_from_sub_path`. Поэтому вынесен на отдельный порт 8443, а `/ha/`
отдаёт 302 на него. При появлении домена — сделать поддомен `ha.<домен>`.

Не сделано:

- **Интеграции HA.** Решено: Алиса остаётся главной, HA ставится параллельно,
  облако Tuya остаётся в контуре — значит перепрошивка и локальные ключи
  не нужны. Дальше: аккаунт на iot.tuya.com → интеграция Tuya (11 устройств)
  → Xiaomi Miio (лампа, пылесос) → long-lived token в конфиг гейта.
- **Zigbee.** Стик ZG-808Z (CC2652P1 + CH340C) — аналог ZBDongle-P,
  идёт в Zigbee2MQTT без перепрошивки. Привязывать по `/dev/serial/by-id/`,
  а не по `ttyUSB0`: имя не гарантировано и может съехать после перезагрузки.
  Датчики TS0205 сейчас на Яндекс Станции; при переносе они уйдут из Алисы —
  устройство живёт только в одной сети Zigbee.
- **Эмбеддинги — заглушка.** Функция `_embed()` строит псевдовектор из sha256:
  хранение и выдача работают, семантического поиска нет. Заменить реальной
  моделью до того, как памятью начнут пользоваться всерьёз.
- **Бэкап Qdrant** — снапшоты по расписанию, хранить у владельца сервера.
- **Доступ снаружи** после переезда: при CGNAT — WireGuard-туннель.
- Датчики дыма и CO: в `write_whitelist` не вносить. Пожарная сигнализация
  должна работать автономно, независимо от интернета, облака и этого кода.
