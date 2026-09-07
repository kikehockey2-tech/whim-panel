#!/usr/bin/env python3
"""
Jala las ventas reales de Loyverse y arma data.json para el panel de Whim.

Corre cada hora vía GitHub Actions (ver .github/workflows/sync-loyverse.yml).
La API key vive SOLO como secreto de GitHub (env var LOYVERSE_TOKEN) —
nunca se escribe en este archivo ni en el resultado (data.json).

Monterrey no tiene horario de verano: siempre es UTC-6, todo el año.
"""
import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

API_BASE = "https://api.loyverse.com/v1.0"
MTY_OFFSET = timedelta(hours=6)  # Monterrey = UTC-6 todo el año, sin horario de verano
MERCADITO_THRESHOLD = 2500  # ventas de un día por encima de esto = día de mercadito/bazar
HISTORY_DAYS = 45  # cuántos días hacia atrás jalar (cubre el historial + 4 semanas para promedios)

# Nombre de producto en Loyverse -> id interno que usa el panel (products[] en index.html).
# OJO: si agregas o renombras un producto en el panel, actualiza este mapa también.
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


def api_get(path, token, params=None):
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


def main():
    token = os.environ.get("LOYVERSE_TOKEN")
    if not token:
        print("Falta LOYVERSE_TOKEN en el ambiente.", file=sys.stderr)
        sys.exit(1)

    now_utc = datetime.now(timezone.utc)
    today_local = (now_utc - MTY_OFFSET).date()
    since_utc = now_utc - timedelta(days=HISTORY_DAYS + 1)

    receipts = fetch_all_receipts(token, since_utc, now_utc)

    # local_date -> {"total": float, "items": {item_name: {"q":.., "r":..}}}
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
            if name not in NAME_TO_ID:
                unmapped_names.add(name)

    # ---------- dailySales (todo el historial jalado) ----------
    daily_sales = {}
    for date, d in sorted(by_day.items()):
        total = round(d["total"])
        kind = "mercadito" if total > MERCADITO_THRESHOLD else "local"
        items_sorted = sorted(d["items"].items(), key=lambda kv: -kv[1]["r"])
        daily_sales[date] = {
            "kind": kind,
            "total": total,
            "items": [
                {"n": name, "q": round(v["q"], 1) if v["q"] % 1 else int(v["q"]), "r": round(v["r"])}
                for name, v in items_sorted
            ],
        }

    # ---------- todaySales: {product_id: cantidad} de hoy (en vivo, puede ir a la mitad) ----------
    today_key = today_local.isoformat()
    today_sales = {}
    if today_key in by_day:
        for name, v in by_day[today_key]["items"].items():
            pid = NAME_TO_ID.get(name)
            if pid:
                today_sales[pid] = round(v["q"], 1) if v["q"] % 1 else int(v["q"])

    # ---------- trend: últimos 7 días reales, terminando hoy ----------
    trend = []
    trend_tags = []
    for i in range(6, -1, -1):
        d = today_local - timedelta(days=i)
        dstr = d.isoformat()
        trend.append(daily_sales.get(dstr, {}).get("total", 0))
        # Sábado (5) y domingo (6) el local no abre, sin importar si hubo mercadito ese día.
        trend_tags.append("cerrado" if d.weekday() in (5, 6) and daily_sales.get(dstr, {}).get("kind") != "mercadito" else None)

    # ---------- weeklySales: promedio de UNIDADES por semana, por producto ----------
    # Días "local" entre semana (lun-vie, sin mercadito) de las últimas 4 semanas.
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
            pid = NAME_TO_ID.get(it["n"])
            if pid:
                weekly_units[pid] = weekly_units.get(pid, 0) + it["q"]
    weeks_counted = max(weekday_local_days / 5, 1)
    weekly_sales = {pid: round(q / weeks_counted, 1) for pid, q in weekly_units.items()}

    # ---------- costos reales por producto (opcional, del catálogo de Loyverse) ----------
    try:
        cost_by_name = fetch_item_costs(token)
        product_cost = {NAME_TO_ID[name]: cost for name, cost in cost_by_name.items() if name in NAME_TO_ID}
    except Exception as e:
        print(f"Aviso: no se pudo refrescar costos de /items: {e}", file=sys.stderr)
        product_cost = {}

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

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    if unmapped_names:
        print("Aviso: productos en Loyverse sin id interno (agrégalos a NAME_TO_ID):", file=sys.stderr)
        for n in sorted(unmapped_names):
            print(f"  - {n}", file=sys.stderr)

    print(f"OK: {len(daily_sales)} días, hoy={today_key}, {len(receipts)} recibos procesados.")


if __name__ == "__main__":
    main()
