#!/usr/bin/env python3
"""
Keenetic — SSH-опрос метрик для Prometheus (textfile collector).
Ничего не устанавливает на роутере, только штатные show-команды.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

KEENETIC_CFG = Path("/opt/homegate/config/keenetic.json")
OUT_FILE = Path("/var/lib/node_exporter/textfile_collector/keenetic.prom")
TMP_FILE = OUT_FILE.with_suffix(".prom.tmp")

WIFI_APS = [
    ("WifiMaster0/AccessPoint0", "HomeAlone", "2.4"),
    ("WifiMaster0/AccessPoint2", "Safin_VPN_Only", "2.4"),
    ("WifiMaster1/AccessPoint0", "HomeAlone5", "5"),
]
WAN_IFACE = "ISP"


def load_cfg():
    return json.loads(KEENETIC_CFG.read_text(encoding="utf-8"))


def ssh_cmd(cfg, command, timeout=10):
    args = [
        "sshpass", "-p", cfg["password"],
        "ssh", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
        "-p", str(cfg["port"]), "{}@{}".format(cfg["user"], cfg["host"]),
        command,
    ]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.stdout


def parse_kv_block(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


def get_wan_stats(cfg):
    d = parse_kv_block(ssh_cmd(cfg, "show interface {} stat".format(WAN_IFACE)))
    link = parse_kv_block(ssh_cmd(cfg, "show interface {}".format(WAN_IFACE)))
    return {
        "rx_bytes": int(d.get("rxbytes", 0) or 0),
        "tx_bytes": int(d.get("txbytes", 0) or 0),
        "rx_speed": int(d.get("rxspeed", 0) or 0),
        "tx_speed": int(d.get("txspeed", 0) or 0),
        "rx_errors": int(d.get("rxerrors", 0) or 0),
        "tx_errors": int(d.get("txerrors", 0) or 0),
        "up": 1 if link.get("state") == "up" else 0,
    }


def get_wifi_aps(cfg):
    result = []
    for iface, name, band in WIFI_APS:
        d = parse_kv_block(ssh_cmd(cfg, "show interface {}".format(iface)))
        result.append({
            "iface": iface, "name": name, "band": band,
            "up": 1 if d.get("state") == "up" else 0,
        })
    return result


def get_wifi_clients(cfg):
    text = ssh_cmd(cfg, "show associations")
    clients = []
    cur = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("station:"):
            if cur:
                clients.append(cur)
            cur = {}
            continue
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        cur[k.strip()] = v.strip()
    if cur:
        clients.append(cur)
    return clients


def get_lte_status(cfg):
    d = parse_kv_block(ssh_cmd(cfg, "show interface UsbQmi0"))
    return {"up": 1 if d.get("state") == "up" else 0}


def band_for_ap(ap_iface):
    for iface, name, band in WIFI_APS:
        if iface == ap_iface:
            return band, name
    return "unknown", ap_iface


def main():
    t0 = time.time()
    lines = []
    try:
        cfg = load_cfg()
        wan = get_wan_stats(cfg)
        aps = get_wifi_aps(cfg)
        clients = get_wifi_clients(cfg)
        lte = get_lte_status(cfg)

        lines.append("# HELP keenetic_scrape_success Успешен ли опрос роутера (1/0)")
        lines.append("# TYPE keenetic_scrape_success gauge")
        lines.append("keenetic_scrape_success 1")

        lines.append("# HELP keenetic_wan_up Состояние WAN-интерфейса (1=up)")
        lines.append("# TYPE keenetic_wan_up gauge")
        lines.append('keenetic_wan_up{{interface="{}"}} {}'.format(WAN_IFACE, wan["up"]))

        lines.append("# HELP keenetic_wan_rx_bytes_total Принято байт на WAN с загрузки роутера")
        lines.append("# TYPE keenetic_wan_rx_bytes_total counter")
        lines.append('keenetic_wan_rx_bytes_total{{interface="{}"}} {}'.format(WAN_IFACE, wan["rx_bytes"]))

        lines.append("# HELP keenetic_wan_tx_bytes_total Отправлено байт на WAN с загрузки роутера")
        lines.append("# TYPE keenetic_wan_tx_bytes_total counter")
        lines.append('keenetic_wan_tx_bytes_total{{interface="{}"}} {}'.format(WAN_IFACE, wan["tx_bytes"]))

        lines.append("# HELP keenetic_wan_rx_speed_bps Текущая скорость приёма, бит/с")
        lines.append("# TYPE keenetic_wan_rx_speed_bps gauge")
        lines.append('keenetic_wan_rx_speed_bps{{interface="{}"}} {}'.format(WAN_IFACE, wan["rx_speed"]))

        lines.append("# HELP keenetic_wan_tx_speed_bps Текущая скорость отдачи, бит/с")
        lines.append("# TYPE keenetic_wan_tx_speed_bps gauge")
        lines.append('keenetic_wan_tx_speed_bps{{interface="{}"}} {}'.format(WAN_IFACE, wan["tx_speed"]))

        lines.append("# HELP keenetic_wifi_ap_up Состояние точки доступа Wi-Fi (1=up)")
        lines.append("# TYPE keenetic_wifi_ap_up gauge")
        for ap in aps:
            lines.append('keenetic_wifi_ap_up{{ssid="{}",band="{}"}} {}'.format(
                ap["name"], ap["band"], ap["up"]))

        counts = {}
        for c in clients:
            band, _ = band_for_ap(c.get("ap", ""))
            counts[band] = counts.get(band, 0) + 1

        lines.append("# HELP keenetic_wifi_clients Число подключённых клиентов по диапазону")
        lines.append("# TYPE keenetic_wifi_clients gauge")
        for _, _, band in WIFI_APS:
            lines.append('keenetic_wifi_clients{{band="{}"}} {}'.format(band, counts.get(band, 0)))

        lines.append("# HELP keenetic_wifi_client_rssi_dbm Уровень сигнала клиента, дБм")
        lines.append("# TYPE keenetic_wifi_client_rssi_dbm gauge")
        for c in clients:
            mac = c.get("mac", "unknown")
            band, ssid = band_for_ap(c.get("ap", ""))
            try:
                rssi = int(c.get("rssi", "0") or 0)
            except ValueError:
                rssi = 0
            lines.append('keenetic_wifi_client_rssi_dbm{{mac="{}",ssid="{}"}} {}'.format(
                mac, ssid, rssi))

        lines.append("# HELP keenetic_lte_up Состояние LTE-резерва (1=активен)")
        lines.append("# TYPE keenetic_lte_up gauge")
        lines.append("keenetic_lte_up {}".format(lte["up"]))

    except Exception as e:
        lines = [
            "# HELP keenetic_scrape_success Успешен ли опрос роутера (1/0)",
            "# TYPE keenetic_scrape_success gauge",
            "keenetic_scrape_success 0",
        ]
        print("ошибка опроса роутера: {}".format(e), file=sys.stderr)

    lines.append("# HELP keenetic_scrape_duration_seconds Сколько занял опрос роутера")
    lines.append("# TYPE keenetic_scrape_duration_seconds gauge")
    lines.append("keenetic_scrape_duration_seconds {:.3f}".format(time.time() - t0))

    TMP_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    TMP_FILE.replace(OUT_FILE)


if __name__ == "__main__":
    main()
