#!/usr/bin/env python3
"""Backend histórico de Loyverse -> data/whim.db (SQLite).

Primera corrida (base vacía, o FULL=1): baja todos los recibos.
Después: incremental con updated_at_min = último updated_at − 2 días,
para alcanzar cancelaciones y reembolsos que llegan tarde.
El catálogo se refresca completo en cada corrida. El inventario corriente
se reemplaza, y además se guarda una foto del día en inventory_snapshots
(no se borran los días anteriores).

Hora local: America/Monterrey (IANA). Desde 2022 es UTC−6 todo el año;
en veranos anteriores el reloj era UTC−5 y la zona lo respeta.

El token entra solo por la variable LOYVERSE_TOKEN. No se imprime, no se
escribe en la base ni en el log. No toca data.json.

El repo es público. Por eso el raw de clientes no guarda correo, teléfono
ni dirección, y el raw de empleados no guarda correo ni teléfono. El nombre
sí se conserva para cruzar recibos. payment_details (tarjeta enmascarada,
códigos del gateway) no se guarda: la forma de pago queda en payments.
"""
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

API = "https://api.loyverse.com/v1.0"
DEFAULT_DB = "data/whim.db"
PAGE_LIMIT = 250
MAX_PAGES = 20000
MIN_INTERVAL = 1.1

# endpoint -> (clave JSON, tabla)
CATALOG = {
    "/items": ("items", "items"),
    "/categories": ("categories", "categories"),
    "/employees": ("employees", "employees"),
    "/stores": ("stores", "stores"),
    "/payment_types": ("payment_types", "payment_types"),
    "/customers": ("customers", "customers"),
    "/inventory": ("inventory_levels", "inventory"),
    "/discounts": ("discounts", "discounts"),
    "/taxes": ("taxes", "taxes"),
    "/modifiers": ("modifiers", "modifiers"),
    "/pos_devices": ("pos_devices", "pos_devices"),
    "/suppliers": ("suppliers", "suppliers"),
    "/shifts": ("shifts", "shifts"),
}
CATALOG_TABLES = {table for _, table in CATALOG.values()}

CUSTOMER_DROP = {"email", "phone", "phone_number", "address", "city", "region", "postal_code", "note"}
EMPLOYEE_DROP = {"email", "phone_number"}

try:
    MTY = ZoneInfo("America/Monterrey")
except Exception:
    MTY = timezone(timedelta(hours=-6))

_NEXT_REQUEST = 0.0


class ApiError(RuntimeError):
    def __init__(self, path, code, detail=""):
        self.path = path
        self.code = code
        if detail:
            msg = f"{path} {detail}" if not code else f"{path} HTTP {code}: {detail}"
        elif code:
            msg = f"{path} HTTP {code}"
        else:
            msg = f"{path} fallo de red"
        super().__init__(msg)


def db_path():
    return os.environ.get("WHIM_DB", DEFAULT_DB)


def full_requested():
    return os.environ.get("FULL", "0").strip() == "1"


def pace():
    global _NEXT_REQUEST
    now = time.monotonic()
    if now < _NEXT_REQUEST:
        time.sleep(_NEXT_REQUEST - now)
    _NEXT_REQUEST = time.monotonic() + MIN_INTERVAL


def redact(text, token):
    if not text:
        return ""
    if token:
        text = text.replace(token, "[redacted]")
    return " ".join(text.split())[:180]


def get(path, token, params):
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{API}{path}?{query}" if query else f"{API}{path}"
    delay = 2
    for attempt in range(6):
        pace()
        try:
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = redact(exc.read().decode("utf-8", "replace"), token)
            except Exception:
                detail = ""
            if exc.code in (401, 403):
                raise ApiError(path, exc.code) from None
            if exc.code in (429, 500, 502, 503, 504) and attempt < 5:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    wait = float(retry_after) if retry_after else delay
                except ValueError:
                    wait = delay
                time.sleep(min(max(wait, 1), 120))
                delay = min(delay * 2, 60)
                continue
            raise ApiError(path, exc.code, detail) from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt < 5:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise ApiError(path, 0) from None
    raise ApiError(path, 0)


def paginate(path, key, token, params=None):
    cursor = None
    seen = set()
    for page in range(MAX_PAGES):
        query = dict(params or {})
        query["limit"] = PAGE_LIMIT
        if cursor:
            query["cursor"] = cursor
        payload = get(path, token, query)
        if not isinstance(payload, dict):
            raise ApiError(path, 0)
        if page == 0 and key not in payload:
            raise ApiError(path, 0, f"respuesta sin clave {key}")
        rows = payload.get(key) or []
        yield from rows
        cursor = payload.get("cursor")
        if not cursor or cursor in seen:
            return
        seen.add(cursor)
    raise ApiError(path, 0)


def parse_utc(iso):
    text = iso.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def format_api_time(moment):
    moment = moment.astimezone(timezone.utc)
    millis = moment.microsecond // 1000
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"


def to_local(iso):
    """Fecha, HH:MM:SS y hora 0-23 en America/Monterrey."""
    moment = parse_utc(iso).astimezone(MTY)
    return moment.date().isoformat(), moment.strftime("%H:%M:%S"), moment.hour


def local_text(iso):
    if not iso:
        return None
    day, clock, _hour = to_local(iso)
    return f"{day} {clock}"


def num(value):
    if value is None or value == "":
        return None
    return float(value)


def display_name(row):
    if row.get("name"):
        return row["name"]
    if row.get("item_name"):
        return row["item_name"]
    parts = [row.get("first_name") or "", row.get("last_name") or ""]
    full = " ".join(part for part in parts if part)
    return full or None


def resource_id(row):
    if row.get("id"):
        return str(row["id"])
    variant_id = row.get("variant_id")
    store_id = row.get("store_id")
    if variant_id or store_id:
        return f"{variant_id}:{store_id}"
    digest = hashlib.sha256(
        json.dumps(row, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    return digest[:32]


def without_keys(row, drop):
    return {key: value for key, value in row.items() if key not in drop}


def scrub_catalog_row(table, row):
    if table == "customers":
        return without_keys(row, CUSTOMER_DROP)
    if table == "employees":
        return without_keys(row, EMPLOYEE_DROP)
    return row


def scrub_receipt(receipt):
    stored = dict(receipt)
    payments = []
    for payment in receipt.get("payments") or []:
        payments.append(without_keys(payment, {"payment_details"}))
    stored["payments"] = payments
    return stored


def money_text(amount):
    if amount is None:
        return ""
    return f"{float(amount):.2f}"


def discounts_text(discounts):
    parts = []
    for discount in discounts or []:
        name = discount.get("name") or discount.get("type") or "descuento"
        amount = discount.get("money_amount")
        percentage = discount.get("percentage")
        extra = money_text(amount)
        if percentage is not None:
            extra = f"{percentage}% {extra}".strip()
        parts.append(f"{name} {extra}".strip())
    return "; ".join(parts)


def modifiers_text(modifiers):
    parts = []
    for modifier in modifiers or []:
        name = modifier.get("name") or "extra"
        option = modifier.get("option")
        if option:
            parts.append(f"{name}: {option}")
        else:
            parts.append(name)
    return "; ".join(parts)


def tax_sum(line):
    total = 0.0
    seen = False
    for tax in line.get("line_taxes") or []:
        if tax.get("money_amount") is not None:
            total += float(tax["money_amount"])
            seen = True
    return total if seen else None


def payments_summary(payments):
    parts = []
    for payment in payments or []:
        label = payment.get("name") or payment.get("type") or "pago"
        parts.append(f"{label} {money_text(payment.get('money_amount'))}".strip())
    return "; ".join(parts)


def variant_label(variant):
    values = [variant.get(f"option{i}_value") for i in (1, 2, 3)]
    values = [value for value in values if value]
    if values:
        return " / ".join(values)
    return variant.get("variant_name")


def variant_price(variant):
    if variant.get("default_price") is not None:
        return num(variant.get("default_price"))
    for store in variant.get("stores") or []:
        if store.get("price") is not None:
            return num(store.get("price"))
    return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
  receipt_number TEXT PRIMARY KEY,
  receipt_type TEXT,
  refund_for TEXT,
  order_name TEXT,
  receipt_date TEXT,
  local_date TEXT,
  local_time TEXT,
  local_hour INTEGER,
  created_at TEXT,
  updated_at TEXT,
  cancelled_at TEXT,
  source TEXT,
  store_id TEXT,
  employee_id TEXT,
  customer_id TEXT,
  pos_device_id TEXT,
  dining_option TEXT,
  note TEXT,
  total_money REAL,
  total_discount REAL,
  total_tax REAL,
  tip REAL,
  surcharge REAL,
  points_earned REAL,
  points_deducted REAL,
  points_balance REAL,
  payments_summary TEXT,
  raw TEXT
);
CREATE TABLE IF NOT EXISTS line_items (
  receipt_number TEXT NOT NULL,
  idx INTEGER NOT NULL,
  line_id TEXT,
  item_id TEXT,
  variant_id TEXT,
  item_name TEXT,
  variant_name TEXT,
  sku TEXT,
  quantity REAL,
  price REAL,
  gross_total_money REAL,
  total_money REAL,
  total_discount REAL,
  tax_money REAL,
  cost REAL,
  cost_total REAL,
  line_note TEXT,
  discounts_text TEXT,
  modifiers_text TEXT,
  PRIMARY KEY (receipt_number, idx)
);
CREATE TABLE IF NOT EXISTS payments (
  receipt_number TEXT NOT NULL,
  idx INTEGER NOT NULL,
  payment_type_id TEXT,
  name TEXT,
  type TEXT,
  money_amount REAL,
  paid_at TEXT,
  local_paid_at TEXT,
  PRIMARY KEY (receipt_number, idx)
);
CREATE TABLE IF NOT EXISTS line_discounts (
  receipt_number TEXT NOT NULL,
  line_idx INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  discount_id TEXT,
  name TEXT,
  type TEXT,
  percentage REAL,
  money_amount REAL,
  PRIMARY KEY (receipt_number, line_idx, idx)
);
CREATE TABLE IF NOT EXISTS receipt_discounts (
  receipt_number TEXT NOT NULL,
  idx INTEGER NOT NULL,
  discount_id TEXT,
  name TEXT,
  type TEXT,
  percentage REAL,
  money_amount REAL,
  PRIMARY KEY (receipt_number, idx)
);
CREATE TABLE IF NOT EXISTS variants (
  variant_id TEXT PRIMARY KEY,
  item_id TEXT,
  item_name TEXT,
  variant_name TEXT,
  sku TEXT,
  category_id TEXT,
  cost REAL,
  default_price REAL,
  deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS inventory_snapshots (
  snapshot_date TEXT NOT NULL,
  store_id TEXT NOT NULL,
  variant_id TEXT NOT NULL,
  item_id TEXT,
  in_stock REAL,
  updated_at TEXT,
  raw TEXT,
  PRIMARY KEY (snapshot_date, store_id, variant_id)
);
CREATE TABLE IF NOT EXISTS sync_log (
  run_at TEXT,
  mode TEXT,
  receipts_upserted INTEGER,
  snapshot_date TEXT,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_receipts_local ON receipts(local_date, local_hour);
CREATE INDEX IF NOT EXISTS idx_receipts_updated ON receipts(updated_at);
CREATE INDEX IF NOT EXISTS idx_receipts_store ON receipts(store_id);
CREATE INDEX IF NOT EXISTS idx_receipts_employee ON receipts(employee_id);
CREATE INDEX IF NOT EXISTS idx_line_items_variant ON line_items(variant_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON inventory_snapshots(snapshot_date);
"""

VIEWS = """
DROP VIEW IF EXISTS daily_sales_by_item;
DROP VIEW IF EXISTS sales_by_hour;
DROP VIEW IF EXISTS daily_sales_by_employee;
DROP VIEW IF EXISTS daily_sales_by_payment;
DROP VIEW IF EXISTS daily_discounts;
DROP VIEW IF EXISTS refunds;
DROP VIEW IF EXISTS receipt_detail;
DROP VIEW IF EXISTS inventory_by_day;

CREATE VIEW daily_sales_by_item AS
SELECT
  r.local_date AS sale_date,
  r.store_id,
  MAX(st.name) AS store_name,
  li.item_id,
  li.variant_id,
  li.item_name,
  li.variant_name,
  li.sku,
  MAX(cat.name) AS category_name,
  ROUND(SUM(CASE WHEN r.receipt_type = 'SALE' THEN li.quantity ELSE 0 END), 4) AS qty_sold,
  ROUND(SUM(CASE WHEN r.receipt_type = 'REFUND' THEN li.quantity ELSE 0 END), 4) AS qty_refunded,
  ROUND(SUM(CASE WHEN r.receipt_type = 'REFUND' THEN -li.quantity ELSE li.quantity END), 4) AS net_qty,
  ROUND(SUM(CASE WHEN r.receipt_type = 'SALE' THEN li.total_money ELSE 0 END), 2) AS sales_money,
  ROUND(SUM(CASE WHEN r.receipt_type = 'REFUND' THEN li.total_money ELSE 0 END), 2) AS refund_money,
  ROUND(SUM(CASE WHEN r.receipt_type = 'REFUND' THEN -li.total_money ELSE li.total_money END), 2) AS net_money,
  ROUND(SUM(CASE WHEN r.receipt_type = 'SALE' THEN COALESCE(li.total_discount, 0) ELSE 0 END), 2) AS discount_money,
  ROUND(SUM(CASE WHEN r.receipt_type = 'REFUND' THEN -COALESCE(li.total_discount, 0)
                 ELSE COALESCE(li.total_discount, 0) END), 2) AS net_discount,
  ROUND(SUM(CASE WHEN r.receipt_type = 'REFUND' THEN -COALESCE(li.cost_total, 0)
                 ELSE COALESCE(li.cost_total, 0) END), 2) AS net_cost,
  COUNT(DISTINCT r.receipt_number) AS receipt_count
FROM line_items li
JOIN receipts r ON r.receipt_number = li.receipt_number
LEFT JOIN stores st ON st.id = r.store_id
LEFT JOIN variants v ON v.variant_id = li.variant_id
LEFT JOIN categories cat ON cat.id = v.category_id
WHERE r.cancelled_at IS NULL
GROUP BY r.local_date, r.store_id, li.item_id, li.variant_id, li.item_name, li.variant_name, li.sku;

CREATE VIEW sales_by_hour AS
SELECT
  r.local_date AS sale_date,
  r.local_hour AS hour_local,
  printf('%02d:00', r.local_hour) AS hour_label,
  r.store_id,
  MAX(st.name) AS store_name,
  SUM(CASE WHEN r.receipt_type = 'SALE' AND r.cancelled_at IS NULL THEN 1 ELSE 0 END) AS sales_count,
  SUM(CASE WHEN r.receipt_type = 'REFUND' AND r.cancelled_at IS NULL THEN 1 ELSE 0 END) AS refund_count,
  SUM(CASE WHEN r.cancelled_at IS NOT NULL THEN 1 ELSE 0 END) AS cancelled_count,
  ROUND(SUM(CASE WHEN r.receipt_type = 'SALE' AND r.cancelled_at IS NULL THEN r.total_money ELSE 0 END), 2) AS sales_money,
  ROUND(SUM(CASE WHEN r.receipt_type = 'REFUND' AND r.cancelled_at IS NULL THEN r.total_money ELSE 0 END), 2) AS refund_money,
  ROUND(SUM(CASE
              WHEN r.cancelled_at IS NOT NULL THEN 0
              WHEN r.receipt_type = 'REFUND' THEN -r.total_money
              ELSE r.total_money
            END), 2) AS net_money,
  ROUND(SUM(CASE
              WHEN r.cancelled_at IS NOT NULL THEN 0
              WHEN r.receipt_type = 'REFUND' THEN -COALESCE(r.total_discount, 0)
              ELSE COALESCE(r.total_discount, 0)
            END), 2) AS net_discount
FROM receipts r
LEFT JOIN stores st ON st.id = r.store_id
GROUP BY r.local_date, r.local_hour, r.store_id;

CREATE VIEW daily_sales_by_employee AS
SELECT
  r.local_date AS sale_date,
  r.store_id,
  MAX(st.name) AS store_name,
  r.employee_id,
  MAX(e.name) AS employee_name,
  SUM(CASE WHEN r.receipt_type = 'SALE' THEN 1 ELSE 0 END) AS sales_count,
  SUM(CASE WHEN r.receipt_type = 'REFUND' THEN 1 ELSE 0 END) AS refund_count,
  ROUND(SUM(CASE WHEN r.receipt_type = 'REFUND' THEN -r.total_money ELSE r.total_money END), 2) AS net_money,
  ROUND(SUM(CASE WHEN r.receipt_type = 'REFUND' THEN -COALESCE(r.total_discount, 0)
                 ELSE COALESCE(r.total_discount, 0) END), 2) AS net_discount
FROM receipts r
LEFT JOIN stores st ON st.id = r.store_id
LEFT JOIN employees e ON e.id = r.employee_id
WHERE r.cancelled_at IS NULL
GROUP BY r.local_date, r.store_id, r.employee_id;

CREATE VIEW daily_sales_by_payment AS
SELECT
  r.local_date AS sale_date,
  r.store_id,
  MAX(st.name) AS store_name,
  p.payment_type_id,
  p.name AS payment_name,
  p.type AS payment_type,
  SUM(CASE WHEN r.receipt_type = 'SALE' THEN 1 ELSE 0 END) AS sales_count,
  SUM(CASE WHEN r.receipt_type = 'REFUND' THEN 1 ELSE 0 END) AS refund_count,
  ROUND(SUM(CASE WHEN r.receipt_type = 'REFUND' THEN -p.money_amount ELSE p.money_amount END), 2) AS net_money
FROM payments p
JOIN receipts r ON r.receipt_number = p.receipt_number
LEFT JOIN stores st ON st.id = r.store_id
WHERE r.cancelled_at IS NULL
GROUP BY r.local_date, r.store_id, p.payment_type_id, p.name, p.type;

CREATE VIEW daily_discounts AS
SELECT
  r.local_date AS sale_date,
  r.store_id,
  MAX(st.name) AS store_name,
  d.discount_id,
  d.name AS discount_name,
  d.type AS discount_type,
  COUNT(*) AS line_count,
  ROUND(SUM(CASE WHEN r.receipt_type = 'REFUND' THEN -COALESCE(d.money_amount, 0)
                 ELSE COALESCE(d.money_amount, 0) END), 2) AS net_money
FROM line_discounts d
JOIN receipts r ON r.receipt_number = d.receipt_number
LEFT JOIN stores st ON st.id = r.store_id
WHERE r.cancelled_at IS NULL
GROUP BY r.local_date, r.store_id, d.discount_id, d.name, d.type;

CREATE VIEW refunds AS
SELECT
  r.receipt_number,
  r.refund_for,
  r.local_date AS sale_date,
  r.local_time,
  r.local_hour,
  r.store_id,
  st.name AS store_name,
  r.employee_id,
  e.name AS employee_name,
  r.total_money,
  r.total_discount,
  r.total_tax,
  r.payments_summary,
  r.cancelled_at,
  r.note
FROM receipts r
LEFT JOIN stores st ON st.id = r.store_id
LEFT JOIN employees e ON e.id = r.employee_id
WHERE r.receipt_type = 'REFUND';

CREATE VIEW receipt_detail AS
SELECT
  r.local_date AS sale_date,
  r.local_time,
  r.local_hour,
  r.receipt_number,
  r.receipt_type,
  r.refund_for,
  r.receipt_date,
  r.cancelled_at,
  r.store_id,
  st.name AS store_name,
  r.employee_id,
  e.name AS employee_name,
  r.customer_id,
  cu.name AS customer_name,
  r.dining_option,
  r.note AS receipt_note,
  r.total_money AS receipt_total,
  r.total_discount AS receipt_discount,
  r.total_tax AS receipt_tax,
  r.tip,
  r.surcharge,
  r.payments_summary,
  r.source,
  li.idx AS line_idx,
  li.item_id,
  li.variant_id,
  li.item_name,
  li.variant_name,
  li.sku,
  li.quantity,
  li.price,
  li.gross_total_money,
  li.total_discount AS line_discount,
  li.total_money AS line_total,
  li.tax_money,
  li.cost,
  li.cost_total,
  li.discounts_text,
  li.modifiers_text,
  li.line_note
FROM receipts r
LEFT JOIN line_items li ON li.receipt_number = r.receipt_number
LEFT JOIN stores st ON st.id = r.store_id
LEFT JOIN employees e ON e.id = r.employee_id
LEFT JOIN customers cu ON cu.id = r.customer_id;

CREATE VIEW inventory_by_day AS
SELECT
  s.snapshot_date,
  s.store_id,
  st.name AS store_name,
  s.variant_id,
  COALESCE(s.item_id, v.item_id) AS item_id,
  v.item_name,
  v.variant_name,
  v.sku,
  cat.name AS category_name,
  s.in_stock,
  s.updated_at
FROM inventory_snapshots s
LEFT JOIN stores st ON st.id = s.store_id
LEFT JOIN variants v ON v.variant_id = s.variant_id
LEFT JOIN categories cat ON cat.id = v.category_id;
"""


def setup(db):
    for _key, table in CATALOG.values():
        db.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, name TEXT, raw TEXT)")
    db.executescript(SCHEMA)
    db.executescript(VIEWS)


def replace_catalog(db, table, rows):
    if table not in CATALOG_TABLES:
        raise RuntimeError("tabla no permitida")
    with db:
        db.execute(f"DELETE FROM {table}")
        for row in rows:
            kept = scrub_catalog_row(table, row)
            db.execute(
                f"INSERT INTO {table} (id, name, raw) VALUES (?,?,?)",
                (resource_id(row), display_name(kept), json.dumps(kept, ensure_ascii=False)),
            )


def replace_variants(db, items):
    with db:
        db.execute("DELETE FROM variants")
        for item in items:
            for variant in item.get("variants") or []:
                variant_id = variant.get("variant_id")
                if not variant_id:
                    continue
                db.execute(
                    """INSERT OR REPLACE INTO variants (
                         variant_id, item_id, item_name, variant_name, sku, category_id,
                         cost, default_price, deleted_at
                       ) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        variant_id,
                        variant.get("item_id") or item.get("id"),
                        item.get("item_name"),
                        variant_label(variant),
                        variant.get("sku"),
                        item.get("category_id"),
                        num(variant.get("cost")),
                        variant_price(variant),
                        variant.get("deleted_at") or item.get("deleted_at"),
                    ),
                )


def save_snapshot(db, levels, snapshot_date, item_by_variant):
    saved = 0
    with db:
        for row in levels:
            variant_id = row.get("variant_id")
            store_id = row.get("store_id")
            if not variant_id or not store_id:
                continue
            db.execute(
                """INSERT OR REPLACE INTO inventory_snapshots (
                     snapshot_date, store_id, variant_id, item_id, in_stock, updated_at, raw
                   ) VALUES (?,?,?,?,?,?,?)""",
                (
                    snapshot_date,
                    store_id,
                    variant_id,
                    item_by_variant.get(variant_id),
                    num(row.get("in_stock")),
                    row.get("updated_at"),
                    json.dumps(row, ensure_ascii=False),
                ),
            )
            saved += 1
    return saved


def save_receipt(db, receipt):
    number = receipt.get("receipt_number")
    stamp_src = receipt.get("receipt_date") or receipt.get("created_at") or receipt.get("updated_at")
    if not number or not stamp_src:
        return False
    local_date, local_time, local_hour = to_local(stamp_src)
    stored = scrub_receipt(receipt)
    pay_rows = stored.get("payments") or []
    db.execute(
        """INSERT OR REPLACE INTO receipts (
             receipt_number, receipt_type, refund_for, order_name, receipt_date,
             local_date, local_time, local_hour, created_at, updated_at, cancelled_at,
             source, store_id, employee_id, customer_id, pos_device_id, dining_option,
             note, total_money, total_discount, total_tax, tip, surcharge,
             points_earned, points_deducted, points_balance, payments_summary, raw
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            number,
            stored.get("receipt_type"),
            stored.get("refund_for"),
            stored.get("order"),
            stored.get("receipt_date"),
            local_date,
            local_time,
            local_hour,
            stored.get("created_at"),
            stored.get("updated_at"),
            stored.get("cancelled_at"),
            stored.get("source"),
            stored.get("store_id"),
            stored.get("employee_id"),
            stored.get("customer_id"),
            stored.get("pos_device_id"),
            stored.get("dining_option"),
            stored.get("note"),
            num(stored.get("total_money")),
            num(stored.get("total_discount")),
            num(stored.get("total_tax")),
            num(stored.get("tip")),
            num(stored.get("surcharge")),
            num(stored.get("points_earned")),
            num(stored.get("points_deducted")),
            num(stored.get("points_balance")),
            payments_summary(pay_rows),
            json.dumps(stored, ensure_ascii=False),
        ),
    )
    db.execute("DELETE FROM line_items WHERE receipt_number = ?", (number,))
    db.execute("DELETE FROM payments WHERE receipt_number = ?", (number,))
    db.execute("DELETE FROM line_discounts WHERE receipt_number = ?", (number,))
    db.execute("DELETE FROM receipt_discounts WHERE receipt_number = ?", (number,))
    for index, line in enumerate(stored.get("line_items") or []):
        db.execute(
            """INSERT INTO line_items (
                 receipt_number, idx, line_id, item_id, variant_id, item_name, variant_name,
                 sku, quantity, price, gross_total_money, total_money, total_discount,
                 tax_money, cost, cost_total, line_note, discounts_text, modifiers_text
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                number,
                index,
                line.get("id"),
                line.get("item_id"),
                line.get("variant_id"),
                line.get("item_name"),
                line.get("variant_name"),
                line.get("sku"),
                num(line.get("quantity")),
                num(line.get("price")),
                num(line.get("gross_total_money")),
                num(line.get("total_money")),
                num(line.get("total_discount")),
                tax_sum(line),
                num(line.get("cost")),
                num(line.get("cost_total")),
                line.get("line_note"),
                discounts_text(line.get("line_discounts")),
                modifiers_text(line.get("line_modifiers")),
            ),
        )
        for discount_index, discount in enumerate(line.get("line_discounts") or []):
            db.execute(
                """INSERT INTO line_discounts (
                     receipt_number, line_idx, idx, discount_id, name, type, percentage, money_amount
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    number,
                    index,
                    discount_index,
                    discount.get("id"),
                    discount.get("name"),
                    discount.get("type"),
                    num(discount.get("percentage")),
                    num(discount.get("money_amount")),
                ),
            )
    for index, payment in enumerate(pay_rows):
        db.execute(
            """INSERT INTO payments (
                 receipt_number, idx, payment_type_id, name, type, money_amount, paid_at, local_paid_at
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                number,
                index,
                payment.get("payment_type_id"),
                payment.get("name"),
                payment.get("type"),
                num(payment.get("money_amount")),
                payment.get("paid_at"),
                local_text(payment.get("paid_at")),
            ),
        )
    for index, discount in enumerate(stored.get("total_discounts") or []):
        db.execute(
            """INSERT INTO receipt_discounts (
                 receipt_number, idx, discount_id, name, type, percentage, money_amount
               ) VALUES (?,?,?,?,?,?,?)""",
            (
                number,
                index,
                discount.get("id"),
                discount.get("name"),
                discount.get("type"),
                num(discount.get("percentage")),
                num(discount.get("money_amount")),
            ),
        )
    return True


def receipt_query(db, full):
    """Sin filtro en el backfill. En incremental, updated_at_min con 2 días de traslape."""
    if full:
        return {}, "backfill"
    last = db.execute("SELECT MAX(updated_at) FROM receipts").fetchone()[0]
    if not last:
        last = db.execute("SELECT MAX(receipt_date) FROM receipts").fetchone()[0]
    if not last:
        return {}, "backfill"
    since = parse_utc(last) - timedelta(days=2)
    return {"updated_at_min": format_api_time(since)}, "incremental"


def snapshot_today():
    return datetime.now(MTY).date().isoformat()


def load_catalog(db, token):
    warnings = []
    inventory_rows = None
    for path, (key, table) in CATALOG.items():
        try:
            rows = list(paginate(path, key, token))
        except ApiError as exc:
            if exc.code in (401, 403):
                print("Loyverse rechazó el token", file=sys.stderr)
                sys.exit(1)
            warnings.append(f"{table}: {exc}")
            print(f"Aviso {table}: {exc}", file=sys.stderr)
            continue
        replace_catalog(db, table, rows)
        if table == "items":
            replace_variants(db, rows)
        if table == "inventory":
            inventory_rows = rows
        print(f"{table}: {len(rows)}", flush=True)
    return inventory_rows, warnings


def main():
    token = os.environ.get("LOYVERSE_TOKEN")
    if not token or not token.strip():
        print("Falta LOYVERSE_TOKEN", file=sys.stderr)
        sys.exit(1)
    token = token.strip()
    path = db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA journal_mode=DELETE")
        setup(db)
        snapshot_date = snapshot_today()
        inventory_rows, warnings = load_catalog(db, token)
        item_by_variant = dict(db.execute("SELECT variant_id, item_id FROM variants"))
        snapshot_count = 0
        if inventory_rows:
            snapshot_count = save_snapshot(db, inventory_rows, snapshot_date, item_by_variant)
        print(f"inventory_snapshots {snapshot_date}: {snapshot_count}", flush=True)

        params, mode = receipt_query(db, full_requested())
        saved = 0
        skipped = 0
        try:
            for receipt in paginate("/receipts", "receipts", token, params):
                if save_receipt(db, receipt):
                    saved += 1
                else:
                    skipped += 1
                if saved and saved % 1000 == 0:
                    db.commit()
                    print(f"recibos: {saved}", flush=True)
            db.commit()
        except ApiError as exc:
            db.rollback()
            if exc.code in (401, 403):
                print("Loyverse rechazó el token", file=sys.stderr)
            else:
                print(f"Error al bajar recibos: {exc}", file=sys.stderr)
            sys.exit(1)
        except Exception:
            db.rollback()
            raise

        note = f"snapshots={snapshot_count}; avisos={len(warnings)}; omitidos={skipped}"
        db.execute(
            "INSERT INTO sync_log (run_at, mode, receipts_upserted, snapshot_date, note) VALUES (?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), mode, saved, snapshot_date, note),
        )
        db.commit()
        total, low, high = db.execute(
            "SELECT COUNT(*), MIN(local_date), MAX(local_date) FROM receipts"
        ).fetchone()
        print(
            f"OK {mode}: {saved} recibos bajados esta corrida; total en base {total}; rango {low} a {high}",
            flush=True,
        )
        db.execute("VACUUM")
    finally:
        db.close()


if __name__ == "__main__":
    main()
