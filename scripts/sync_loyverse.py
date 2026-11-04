#!/usr/bin/env python3
"""
Jala las ventas reales de Loyverse y arma data.json para el panel de Whim.

Lo corre GitHub Actions (.github/workflows/sync-loyverse.yml): cada 15 minutos
cuando GitHub sí dispara ese cron, más dos anclas diarias para que el panel
no se quede sin una corrida al día. La API key vive SOLO como secreto
(env var LOYVERSE_TOKEN). Nunca se escribe en este archivo, en el log ni en
data.json.

Monterrey no tiene horario de verano: siempre es UTC-6, todo el año.

Loyverse responde HTTP 402 si pides recibos de más de 31 días y la cuenta no
tiene el add-on de historial ilimitado. La ventana larga se intenta primero;
si llega un 402, se reintenta dentro de ese límite para no congelar data.json.
"""
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API_BASE = "https://api.loyverse.com/v1.0"
MTY_OFFSET = timedelta(hours=6)  # Monterrey = UTC-6 todo el año, sin horario de verano
MERCADITO_THRESHOLD = 2500  # ventas de un día por encima de esto = día de mercadito/bazar
HISTORY_DAYS = 45  # ventana preferida (más el día de margen de abajo)
# 30 días hacia atrás se queda dentro del límite gratis de 31 días.
FREE_HISTORY_DAYS = 30

# Nombre de producto en Loyverse -> id interno que usa el panel (products[] en index.html).
# OJO: si agregas o renombras un producto en el panel, actualiza este mapa también.
# Los acentos y el sufijo " M" (SKU de mercadito) se resuelven en resolve_product_id;
# no hace falta duplicar aquí "Frappe Cafe" ni "Elote Grande M".
NAME_TO_ID = {
    "Elote Grande": "elote_grande",
    "Elote Mediano": "elote_mediano",
    "Whimix Elote Bowl": "whimix_elote",
    "Whimix Fruit Bowl": "whimix_fruit",
    "Whimix Bowl": "whimix_bowl",
    "nieve grande": "nieve_grande",
    "Nieve mediana": "nieve_mediana",
    "Smoothie Myo": "smoothie_myo",
    "Smoothie Cocoa Proteína": "smoothie_cocoa",
    "Smoothie Piñacoco": "smoothie_pinacoco",
    "Smoothie Chocoplátano": "smoothie_chocoplatano",
    "Smoothie Berries": "smoothie_berries",
    "Smoothie Mangonada": "smoothie_mangonada",
    "Smoothie Limon/Fresa": "smoothie_limonfresa",
    "Frappé Café": "frappe_cafe",
    "Frappé Mocha": "frappe_mocha",
    "Frappé Vainilla": "frappe_vainilla",
    "Frappé Chocolate": "frappe_chocolate",
    "Frappé Chai": "frappe_chai",
    "Matcha": "matcha",
    "Bowl Jícama": "bowl_jicama",
    "+ Nieve": "mas_nieve",
    "Brownie": "brownie",
    "Galleta": "galleta",
    "Agua Natural": "agua_natural",
    "Powerade": "powerade",
    "Coca cola": "coca_cola",
    "Aguas Frescas": "aguas_frescas",
    "Grilled Sandwich": "grilled_sandwich",
    "Para llevar": "para_llevar",
}

# Nombre doblado (sin acentos, minúsculas) -> nombre doblado que sí está en el mapa.
# "Bowl Jicam dh" es el mismo Bowl Jícama en el POS (mismo precio de $65); el
# nombre quedó cortado. No hay id de panel para "Suerito" ni para "Smoothie Fresa".
ALIASES = {
    "bowl jicam dh": "bowl jicama",
}


class LoyverseHTTPError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body or ""
        super().__init__(f"HTTP {status}")


def data_path():
    return os.environ.get("WHIM_DATA_PATH", "data.json")


def redact(text, token):
    """Quita el secreto si alguna respuesta lo repitiera. Nunca se imprime."""
    if not text:
        return ""
    if token:
        text = text.replace(token, "[REDACTED]")
    text = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", text, flags=re.IGNORECASE)
    return text.replace("\n", " ").strip()[:300]


def fold_name(name):
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold().strip()


_FOLD_TO_ID = {fold_name(name): pid for name, pid in NAME_TO_ID.items()}


def resolve_product_id(name):
    """Id del panel para un nombre de Loyverse, o None si no hay producto."""
    if name in NAME_TO_ID:
        return NAME_TO_ID[name]
    key = ALIASES.get(fold_name(name), fold_name(name))
    pid = _FOLD_TO_ID.get(key)
    if pid:
        return pid
    if key.endswith(" m"):
        return _FOLD_TO_ID.get(key[:-2].strip())
    return None


def qty_num(q):
    return round(q, 1) if q % 1 else int(q)


def api_get(path, token, params=None):
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise LoyverseHTTPError(e.code, raw) from None


def fetch_all_receipts(token, since_utc, until_utc):
    receipts = []
    cursor = None
    while True:
        params = {
            "created_at_min": since_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "created_at_max": until_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "limit": 250,
        }
        if cursor:
            params["cursor"] = cursor
        data = api_get("/receipts", token, params)
        receipts.extend(data.get("receipts", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return receipts


def explain_http_error(err, token):
    safe = redact(err.body, token)
    detail = f" Cuerpo: {safe}" if safe else ""
    if err.status in (401, 403):
        return (
            "LOYVERSE_TOKEN está configurado pero Loyverse lo rechazó "
            f"(HTTP {err.status}). El secreto es inválido o fue revocado.{detail}"
        )
    if err.status == 402:
        return (
            "LOYVERSE_TOKEN fue aceptado, pero Loyverse respondió HTTP 402 "
            "(historial de más de 31 días sin el add-on, o pago requerido) "
            f"incluso en la ventana de {FREE_HISTORY_DAYS} días.{detail}"
        )
    return f"Error de Loyverse HTTP {err.status}.{detail}"


def fetch_receipts(token, now_utc):
    """Ventana de HISTORY_DAYS; si Loyverse responde 402, reintenta en 30 días."""
    attempts = (
        (HISTORY_DAYS + 1, "larga"),
        (FREE_HISTORY_DAYS, "corta"),
    )
    last_err = None
    for days_back, label in attempts:
        since = now_utc - timedelta(days=days_back)
        try:
            receipts = fetch_all_receipts(token, since, now_utc)
            return receipts, days_back
        except LoyverseHTTPError as err:
            last_err = err
            if err.status == 402 and label == "larga":
                print(
                    "Aviso: Loyverse HTTP 402 en la ventana de "
                    f"{days_back} días ({redact(err.body, token) or 'sin cuerpo'}). "
                    f"Reintento con {FREE_HISTORY_DAYS} días, dentro del límite gratis.",
                    file=sys.stderr,
                )
                continue
            print(explain_http_error(err, token), file=sys.stderr)
            raise SystemExit(1) from None
    print(explain_http_error(last_err, token), file=sys.stderr)
    raise SystemExit(1)


def fetch_item_costs(token):
    """item_name -> costo unitario real, jalado de /items (variants[0].cost)."""
    costs = {}
    cursor = None
    while True:
        params = {"limit": 250}
        if cursor:
            params["cursor"] = cursor
        data = api_get("/items", token, params)
        for item in data.get("items", []):
            variants = item.get("variants") or []
            if variants:
                cost = variants[0].get("cost")
                if cost is not None:
                    costs[item["item_name"]] = cost
        cursor = data.get("cursor")
        if not cursor:
            break
    return costs


def to_local_date(iso_utc):
    """'2026-09-05T01:49:21.000Z' (UTC) -> ('2026-09-04', datetime local)"""
    dt_utc = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    dt_local = dt_utc - MTY_OFFSET
    return dt_local.date().isoformat(), dt_local


def load_previous(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def merge_daily_sales(fresh, previous):
    """Los días recién bajados ganan; los que ya no entran en la ventana se quedan."""
    merged = {}
    if isinstance(previous, dict):
        merged.update(previous)
    merged.update(fresh)
    return dict(sorted(merged.items()))


def build_payload(receipts, cost_by_name, now_utc, previous):
    today_local = (now_utc - MTY_OFFSET).date()
    by_day = {}
    unmapped_names = set()

    for r in receipts:
        if r.get("receipt_type") != "SALE" or r.get("cancelled_at"):
            continue
        local_date, _ = to_local_date(r["receipt_date"])
        day = by_day.setdefault(local_date, {"total": 0.0, "items": {}})
        for li in r.get("line_items", []):
            name = li["item_name"]
            qty = li.get("quantity", 0) or 0
            rev = li.get("total_money", 0) or 0
            entry = day["items"].setdefault(name, {"q": 0.0, "r": 0.0})
            entry["q"] += qty
            entry["r"] += rev
            day["total"] += rev
            if resolve_product_id(name) is None:
                unmapped_names.add(name)

    fresh_daily = {}
    for date, d in sorted(by_day.items()):
        total = round(d["total"])
        kind = "mercadito" if total > MERCADITO_THRESHOLD else "local"
        items_sorted = sorted(d["items"].items(), key=lambda kv: -kv[1]["r"])
        fresh_daily[date] = {
            "kind": kind,
            "total": total,
            "items": [
                {"n": name, "q": qty_num(v["q"]), "r": round(v["r"])}
                for name, v in items_sorted
            ],
        }

    prev_daily = previous.get("dailySales") if isinstance(previous, dict) else None
    daily_sales = merge_daily_sales(fresh_daily, prev_daily)

    today_key = today_local.isoformat()
    raw_today = {}
    if today_key in by_day:
        for name, v in by_day[today_key]["items"].items():
            pid = resolve_product_id(name)
            if pid:
                raw_today[pid] = raw_today.get(pid, 0) + v["q"]
    today_sales = {pid: qty_num(q) for pid, q in raw_today.items()}

    trend = []
    trend_tags = []
    for i in range(6, -1, -1):
        d = today_local - timedelta(days=i)
        dstr = d.isoformat()
        trend.append(daily_sales.get(dstr, {}).get("total", 0))
        # Sábado (5) y domingo (6) el local no abre, sin importar si hubo mercadito ese día.
        weekend = d.weekday() in (5, 6)
        is_market = daily_sales.get(dstr, {}).get("kind") == "mercadito"
        trend_tags.append("cerrado" if weekend and not is_market else None)

    weekly_units = {}
    weekday_local_days = 0
    for i in range(28):
        d = today_local - timedelta(days=i + 1)  # sin contar hoy (día incompleto)
        dstr = d.isoformat()
        if d.weekday() >= 5:
            continue
        day = daily_sales.get(dstr)
        if not day or day["kind"] != "local":
            continue
        weekday_local_days += 1
        for it in day["items"]:
            pid = resolve_product_id(it["n"])
            if pid:
                weekly_units[pid] = weekly_units.get(pid, 0) + it["q"]
    weeks_counted = max(weekday_local_days / 5, 1)
    weekly_sales = {pid: round(q / weeks_counted, 1) for pid, q in weekly_units.items()}

    product_cost = {}
    for name, cost in (cost_by_name or {}).items():
        pid = resolve_product_id(name)
        if pid is not None and cost is not None:
            product_cost[pid] = cost
    if not product_cost and isinstance(previous.get("productCost"), dict):
        product_cost = previous["productCost"]

    out = {
        "generated_at": now_utc.isoformat(),
        "today_local": today_key,
        "dailySales": daily_sales,
        "todaySales": today_sales,
        "trend": trend,
        "trendTags": trend_tags,
        "weeklySales": weekly_sales,
        "productCost": product_cost,
    }
    return out, unmapped_names


def write_payload(out, path, token):
    blob = json.dumps(out, ensure_ascii=False, indent=1)
    if token and token in blob:
        print(
            "Aborto: data.json iba a incluir LOYVERSE_TOKEN. No se escribió el archivo.",
            file=sys.stderr,
        )
        return 1
    with open(path, "w", encoding="utf-8") as f:
        f.write(blob)
        f.write("\n")
    return 0


def main():
    token = os.environ.get("LOYVERSE_TOKEN")
    if not token:
        print(
            "LOYVERSE_TOKEN no está en el ambiente. El secreto del repositorio "
            "falta o este job no lo recibe. No se puede jalar Loyverse.",
            file=sys.stderr,
        )
        return 1

    path = data_path()
    previous = load_previous(path)
    now_utc = datetime.now(timezone.utc)

    try:
        receipts, window_days = fetch_receipts(token, now_utc)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1

    if not receipts and previous.get("dailySales"):
        print(
            "Loyverse respondió sin recibos y data.json ya tiene historial. "
            "No se reescribe el archivo para no borrarlo.",
            file=sys.stderr,
        )
        return 1

    try:
        cost_by_name = fetch_item_costs(token)
    except LoyverseHTTPError as e:
        print(
            f"Aviso: no se pudo refrescar costos de /items: {explain_http_error(e, token)}",
            file=sys.stderr,
        )
        cost_by_name = {}
    except Exception as e:
        print(f"Aviso: no se pudo refrescar costos de /items: {e}", file=sys.stderr)
        cost_by_name = {}

    out, unmapped_names = build_payload(receipts, cost_by_name, now_utc, previous)
    status = write_payload(out, path, token)
    if status != 0:
        return status

    if unmapped_names:
        print("Aviso: productos en Loyverse sin id interno (agrégalos a NAME_TO_ID):", file=sys.stderr)
        for n in sorted(unmapped_names):
            print(f"  - {n}", file=sys.stderr)

    print(
        f"OK: ventana={window_days} días, {len(out['dailySales'])} días en archivo, "
        f"hoy={out['today_local']}, {len(receipts)} recibos procesados."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
