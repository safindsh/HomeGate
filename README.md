# HomeGate

Конфигурация домашнего сервера: Home Assistant, MCP-гейт для Claude,
векторная память, мониторинг, реверс-прокси.

Сервер стоит в частном доме и обслуживает домашнюю автоматику.

---

## Что внутри

```
homeassistant/           Home Assistant (compose + configuration.yaml)
homegate/app/            MCP-гейт: FastAPI, типизированные инструменты
homegate/config/         config.example.json — шаблон (реальный конфиг не в git)
qdrant/                  векторная память (compose)
monitoring/              Prometheus + Grafana + дашборды
nginx/homegate.conf      реверс-прокси, TLS
nginx/ha.conf            Home Assistant на отдельном порту 8443
systemd/                 юниты homegate и node_exporter
docs/README.server.md    эксплуатационная документация машины
```

## Сервисы

| Сервис | Где | Порт | Автозапуск |
|---|---|---|---|
| nginx | нативно | 80, 443, 8443 | systemd |
| node_exporter | нативно | 172.17.0.1:9100 | systemd |
| homegate (MCP) | нативно, venv | 127.0.0.1:8800 | systemd |
| Home Assistant | Docker | 8123 (host network) | compose |
| Qdrant | Docker | 127.0.0.1:6333 | compose |
| Prometheus | Docker | 127.0.0.1:9090 | compose |
| Grafana | Docker | 127.0.0.1:3000 | compose |

Наружу открыты только 22, 80, 443, 8443. Остальное на localhost,
доступ через nginx. Qdrant намеренно не проксируется.

### Точки входа

| Адрес | Что |
|---|---|
| `https://<host>/` | дашборды |
| `https://<host>/grafana/` | Grafana |
| `https://<host>/claude-mcp/health` | MCP-гейт |
| `https://<host>:8443/` | Home Assistant |

**Home Assistant не умеет работать в подкаталоге** — у него нет аналога
`serve_from_sub_path` из Grafana. Через `/ha/` он редиректит на
`/onboarding.html` без префикса и получается 404. Поэтому вынесен на
отдельный порт 8443, а `/ha/` отдаёт 302. При появлении домена правильнее
сделать поддомен `ha.<домен>` на 443.

---

## MCP-гейт: почему не голый shell

Гейт даёт Claude доступ к дому. Ключевое решение — **инструменты
типизированные, а не произвольные shell-команды**.

Причина: дома цена ошибки выше, чем на рабочем сервере. Там упадёт сайт,
здесь останется без отопления живой человек, и чинить некому.

Принципы:

1. **Типизированные инструменты.** При `run_command` модель сама решает,
   какой командой добиться цели, и может ошибиться как угодно.
   При `device_control(entity, action)` максимум — не та лампа.

2. **Чтение свободно, запись по белому списку.** Разобраться в проблеме
   можно без спроса. Менять состояние дома — только для сущностей из
   `homeassistant.write_whitelist`.

3. **Домены `lock`, `climate`, `water_heater`, `valve`,
   `alarm_control_panel` запрещены жёстко** — в коде, не в конфиге.
   Даже если внести их в whitelist, `device_control` откажет. Замки,
   отопление, вода, щит — только руками хозяина.

4. **Роли разделены токенами.** У пользователя нет shell физически:
   `tools/list` его не отдаёт, прямой вызов возвращает отказ.

5. **Аудит.** Каждый вызов shell и каждое управление устройством пишется
   в `audit.log` с именем вызывающего.

### Инструменты

Роль `user` — 7 штук:
`memory_save`, `memory_search`, `memory_list`, `home_state`,
`sensor_history`, `home_anomalies`, `device_control`

Роль `admin` — те же плюс `run_command`, `service_status`.

`home_anomalies` — не «покажи датчики», а «что выглядит не так»:
молчащие больше суток сенсоры, севшие батареи, недоступные устройства.
Служебные сущности HA (бэкапы, TTS, погода) отфильтрованы, недоступные
группируются по устройству — иначе одно отключённое устройство даёт
восемь строк вывода.

---

## Развёртывание с нуля

Порядок важен: сервисы зависят друг от друга.

### 1. База

```bash
dnf install -y epel-release
dnf install -y git vim tmux htop bind-utils wget policycoreutils-python-utils
# Docker CE по инструкции docker.com, затем:
systemctl enable --now docker
```

### 2. Секреты

Скопировать шаблоны и заполнить своими значениями:

```bash
cp homegate/config/config.example.json /opt/homegate/config/config.json
cp monitoring/.env.example /opt/monitoring/.env
cp qdrant/.env.example /opt/qdrant/.env
chmod 600 /opt/homegate/config/config.json /opt/monitoring/.env /opt/qdrant/.env
```

Токены генерировать на месте: `openssl rand -hex 32`.
Не переиспользовать токены из других контуров.

### 3. Сертификат

```bash
mkdir -p /etc/nginx/ssl
openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
  -keyout /etc/nginx/ssl/selfsigned.key -out /etc/nginx/ssl/selfsigned.crt \
  -subj "/CN=homegate.local"
chmod 600 /etc/nginx/ssl/selfsigned.key
```

При появлении домена: `certbot --nginx -d <домен>`.
Путь `/.well-known/acme-challenge/` в конфиге уже проброшен.

### 4. Сервисы

```bash
cd /opt/qdrant && docker compose up -d
cd /opt/monitoring && docker compose up -d
cd /opt/homeassistant && docker compose up -d
systemctl enable --now node_exporter homegate nginx
```

### 5. Home Assistant

Первичная настройка только через браузер: `https://<host>:8443/`,
создать аккаунт владельца. Дальше интеграции добавляются там же —
через командную строку config flow не пройти.

Для метрик в Prometheus нужен long-lived token: профиль → Безопасность →
Токены долгосрочного доступа. Положить в
`/opt/monitoring/prometheus/ha_token` (файл в `.gitignore`).

---

## Интеграции

### Tuya / Smart Life

**Схема авторизации изменилась.** С версии HA 2024.x интеграция не
спрашивает Access ID и Secret — она просит **User Code** из приложения:
*Smart Life → Я → Настройки → Учётная запись и безопасность → Код
пользователя*. Access ID туда не подходит, будет `USERCODE_INCORRECT`.

Аккаунт разработчика на `iot.tuya.com` всё равно нужен:

- Cloud Project с типом **Smart Home**
- Data Center должен совпадать с регионом аккаунта Smart Life
  (для России обычно **Central Europe**). Ошибка здесь = устройства
  просто не появятся
- Service API: минимум `IoT Core`, `Authorization Token Management`,
  `Smart Home Scene Linkage`
- Devices → Link Tuya App Account → QR-код → отсканировать в Smart Life

**Аккаунт бесплатный, но требует продления вручную.** Первый период —
месяц, дальше кнопка *Extend Trial Period* продлевает на полгода. Если
забыть — устройства отвалятся до следующего нажатия. Стоит поставить
напоминание.

### Что не работает через облако Tuya

ИК-передатчики (`Smart IR`, кондиционер, телевизор) приходят как
`unsupported` — облачный API не отдаёт ИК-команды. Нужен локальный путь
или отдельная интеграция.

### Bluetooth

Встроенный адаптер подхватывается HA автоматически. Для этого в compose
нужны `NET_ADMIN` + `NET_RAW` и проброс `/run/dbus`, иначе
`PermissionError`.

**Tuya BLE-устройства локально читаются не все.** Интеграция
`ha_tuya_ble` требует ключ шифрования из облака, а список поддерживаемых
`product_id` невелик. Устройство может отлично ловиться по эфиру, но
нагрузка останется зашифрованной. Проверять по списку в README
интеграции до того, как рассчитывать на локальную работу.

---

## Мониторинг

Prometheus собирает три цели: сам себя, `node_exporter` и Home Assistant.

Эндпоинт `/api/prometheus` **по умолчанию выключен** — без секции
`prometheus:` в `configuration.yaml` отдаёт 404.

Метрики дома приходят с префиксом `hass_`: `hass_sensor_power_w`,
`hass_sensor_energy_kwh`, `hass_binary_sensor_state`, `hass_cover_state`
и другие. Это позволяет строить графики энергопотребления розеток и
историю состояний.

Дашборды в `monitoring/grafana/provisioning/dashboards/`:
`node_exporter_full.json` (31 панель по хосту) и `node.json`
(лёгкая сводка на один экран).

---

## Частые операции

```bash
# статус всего
systemctl status homegate nginx node_exporter
docker ps

# перезапуск гейта после правки кода или конфига
systemctl restart homegate
tail -f /opt/homegate/logs/service.log

# кто что делал
tail -50 /opt/homegate/logs/audit.log

# проверка гейта
curl -s http://127.0.0.1:8800/claude-mcp/health

# цели прометея
curl -s "http://127.0.0.1:9090/api/v1/targets?state=active" | python3 -m json.tool
```

---

## Грабли

- **SELinux Enforcing.** Новые каталоги под логи требуют контекста,
  иначе systemd молча падает с `status=209/STDOUT`:
  `semanage fcontext -a -t var_log_t "/path(/.*)?" && restorecon -Rv /path`
  Для бинд-маунтов в Docker — суффикс `:Z`.

- **nginx 1.20**, не 1.25: `listen 443 ssl http2;` одной строкой,
  отдельная директива `http2 on;` не поддерживается.

- **Grafana в подкаталоге** требует и `GF_SERVER_ROOT_URL`, и
  `serve_from_sub_path`, и `proxy_pass` **без** трейлинг-слеша. Слеш
  срезает префикс, получается цикл редиректов.

- **Дефолтный `server`-блок** вырезан из `nginx.conf`, иначе конфликт
  по `server_name _`.

- **CH340 (Zigbee-стики)** даёт нестабильные имена устройств:
  `ttyUSB0` может стать `ttyUSB1`. Привязывать по
  `/dev/serial/by-id/`, иначе после перезагрузки адаптер потеряется.

---

## Безопасность

- Секретов в репозитории нет и быть не должно. `.gitignore` блокирует
  `config.json`, `*.env`, `*.key`, `*.crt`, `*_token`, логи, venv, storage
- Токены генерировать на месте, не переиспользовать между контурами
- Датчики дыма и CO **не вносить в `write_whitelist`**. Пожарная
  сигнализация должна работать автономно; HA — дополнительный слой
  уведомлений, не замена
- Перед коммитом проверять: `git diff --cached | grep -iE "token|secret|password"`

---

## Что не сделано

- **Эмбеддинги — заглушка.** Функция `_embed()` строит псевдовектор из
  sha256: хранение и выдача работают, семантического поиска нет.
  Заменить реальной моделью до того, как памятью начнут пользоваться
  всерьёз.
- **Бэкап Qdrant** — снапшоты по расписанию, хранить у владельца сервера.
- **Доступ снаружи** после переезда: при CGNAT нужен WireGuard-туннель.
- **`write_whitelist` пуст** — управление устройствами полностью
  запрещено, пока владелец не решит, что можно трогать автоматически.
