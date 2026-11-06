#!/usr/bin/env python3
"""Pruebas del esquema histórico. No llama a Loyverse ni usa el token."""
import importlib.util
import sqlite3
import unittest
from pathlib import Path


def load():
    path = Path(__file__).with_name("backfill_loyverse.py")
    spec = importlib.util.spec_from_file_location("backfill_loyverse", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BackfillSchemaTest(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.mod.setup(self.db)
        self.db.execute(
            "INSERT INTO stores (id, name, raw) VALUES (?,?,?)",
            ("s1", "Local Centro", "{}"),
        )
        self.db.execute(
            "INSERT INTO employees (id, name, raw) VALUES (?,?,?)",
            ("e1", "Ana", "{}"),
        )
        self.db.execute(
            "INSERT INTO customers (id, name, raw) VALUES (?,?,?)",
            ("c1", "Luis", "{}"),
        )
        self.db.execute(
            "INSERT INTO categories (id, name, raw) VALUES (?,?,?)",
            ("cat1", "Elotes", "{}"),
        )
        self.db.execute(
            """INSERT INTO variants (
                 variant_id, item_id, item_name, variant_name, sku, category_id,
                 cost, default_price, deleted_at
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            ("v1", "i1", "Elote Grande", None, "SKU1", "cat1", 20, 70, None),
        )

    def tearDown(self):
        self.db.close()

    def test_monterrey_local_time(self):
        day, clock, hour = self.mod.to_local("2026-09-05T01:49:21.000Z")
        self.assertEqual(day, "2026-09-04")
        self.assertEqual(clock, "19:49:21")
        self.assertEqual(hour, 19)
        # Verano 2020 en Monterrey era UTC-5.
        day, clock, hour = self.mod.to_local("2020-06-23T08:35:47.047Z")
        self.assertEqual((day, clock, hour), ("2020-06-23", "03:35:47", 3))

    def test_incremental_uses_updated_at(self):
        self.mod.save_receipt(self.db, self._sale())
        params, mode = self.mod.receipt_query(self.db, full=False)
        self.assertEqual(mode, "incremental")
        self.assertEqual(params["updated_at_min"], "2026-09-03T01:49:21.000Z")
        self.assertNotIn("created_at_min", params)
        params, mode = self.mod.receipt_query(self.db, full=True)
        self.assertEqual((params, mode), ({}, "backfill"))
        empty = sqlite3.connect(":memory:")
        self.mod.setup(empty)
        self.assertEqual(self.mod.receipt_query(empty, full=False), ({}, "backfill"))
        empty.close()

    def test_views_sales_hour_payments_and_refunds(self):
        self.assertTrue(self.mod.save_receipt(self.db, self._sale()))
        self.assertTrue(self.mod.save_receipt(self.db, self._refund()))
        self.assertTrue(self.mod.save_receipt(self.db, self._cancelled()))
        self.mod.save_receipt(self.db, self._sale())

        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM line_items").fetchone()[0], 3)
        raw = self.db.execute("SELECT raw FROM receipts WHERE receipt_number = 'S-1'").fetchone()[0]
        self.assertNotIn("4242", raw)
        self.assertNotIn("payment_details", raw)
        self.assertNotIn("authorization_code", raw)

        item = self.db.execute("SELECT * FROM daily_sales_by_item").fetchone()
        self.assertEqual(item["sale_date"], "2026-09-04")
        self.assertEqual(item["store_name"], "Local Centro")
        self.assertEqual(item["item_name"], "Elote Grande")
        self.assertEqual(item["category_name"], "Elotes")
        self.assertEqual(item["qty_sold"], 2)
        self.assertEqual(item["qty_refunded"], 1)
        self.assertEqual(item["net_qty"], 1)
        self.assertEqual(item["sales_money"], 140)
        self.assertEqual(item["refund_money"], 70)
        self.assertEqual(item["net_money"], 70)
        self.assertEqual(item["discount_money"], 10)
        self.assertEqual(item["net_discount"], 5)
        self.assertEqual(item["net_cost"], 20)

        hours = {
            row["hour_local"]: row
            for row in self.db.execute("SELECT * FROM sales_by_hour ORDER BY hour_local")
        }
        self.assertEqual(hours[19]["hour_label"], "19:00")
        self.assertEqual(hours[19]["sales_count"], 1)
        self.assertEqual(hours[19]["cancelled_count"], 1)
        self.assertEqual(hours[19]["net_money"], 140)
        self.assertEqual(hours[20]["refund_count"], 1)
        self.assertEqual(hours[20]["net_money"], -70)

        employee = self.db.execute("SELECT * FROM daily_sales_by_employee").fetchone()
        self.assertEqual(employee["employee_name"], "Ana")
        self.assertEqual(employee["sales_count"], 1)
        self.assertEqual(employee["refund_count"], 1)
        self.assertEqual(employee["net_money"], 70)

        payment = self.db.execute("SELECT * FROM daily_sales_by_payment").fetchone()
        self.assertEqual(payment["payment_name"], "Efectivo")
        self.assertEqual(payment["payment_type"], "CASH")
        self.assertEqual(payment["net_money"], 70)

        discount = self.db.execute("SELECT * FROM daily_discounts").fetchone()
        self.assertEqual(discount["discount_name"], "Promo")
        self.assertEqual(discount["net_money"], 5)

        refund = self.db.execute("SELECT * FROM refunds").fetchone()
        self.assertEqual(refund["refund_for"], "S-1")
        self.assertEqual(refund["local_time"], "20:10:00")
        self.assertEqual(refund["employee_name"], "Ana")

        detail = self.db.execute(
            """SELECT * FROM receipt_detail
               WHERE receipt_number = 'S-1' AND line_idx = 0"""
        ).fetchone()
        self.assertEqual(detail["local_time"], "19:49:21")
        self.assertEqual(detail["local_hour"], 19)
        self.assertEqual(detail["employee_name"], "Ana")
        self.assertEqual(detail["customer_name"], "Luis")
        self.assertEqual(detail["payments_summary"], "Efectivo 140.00")
        self.assertEqual(detail["line_discount"], 10)
        self.assertEqual(detail["modifiers_text"], "Toppings: Chile")
        self.assertEqual(detail["discounts_text"], "Promo 10% 10.00")

    def test_inventory_snapshot_keeps_previous_days(self):
        levels_day_one = [{"variant_id": "v1", "store_id": "s1", "in_stock": 5, "updated_at": "2026-11-01T09:00:00.000Z"}]
        self.mod.replace_catalog(self.db, "inventory", levels_day_one)
        self.mod.save_snapshot(self.db, levels_day_one, "2026-11-01", {"v1": "i1"})
        levels_later = [{"variant_id": "v1", "store_id": "s1", "in_stock": 3, "updated_at": "2026-11-01T18:00:00.000Z"}]
        self.mod.replace_catalog(self.db, "inventory", levels_later)
        self.mod.save_snapshot(self.db, levels_later, "2026-11-01", {"v1": "i1"})
        self.mod.save_snapshot(
            self.db,
            [{"variant_id": "v1", "store_id": "s1", "in_stock": 4, "updated_at": "2026-11-02T09:00:00.000Z"}],
            "2026-11-02",
            {"v1": "i1"},
        )
        rows = list(self.db.execute("SELECT * FROM inventory_by_day ORDER BY snapshot_date"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["snapshot_date"], "2026-11-01")
        self.assertEqual(rows[0]["in_stock"], 3)
        self.assertEqual(rows[0]["item_name"], "Elote Grande")
        self.assertEqual(rows[0]["store_name"], "Local Centro")
        self.assertEqual(rows[1]["in_stock"], 4)
        current = self.db.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
        self.assertEqual(current, 1)

    def test_customer_and_employee_contact_fields_are_not_stored(self):
        self.mod.replace_catalog(
            self.db,
            "customers",
            [{
                "id": "c1",
                "name": "Luis",
                "email": "luis@example.com",
                "phone_number": "8112345678",
                "address": "Calle 1",
                "city": "Monterrey",
                "note": "cumpleaños",
                "total_visits": 4,
            }],
        )
        raw = self.db.execute("SELECT raw FROM customers WHERE id = 'c1'").fetchone()[0]
        self.assertIn("Luis", raw)
        self.assertIn("total_visits", raw)
        self.assertNotIn("luis@example.com", raw)
        self.assertNotIn("8112345678", raw)
        self.assertNotIn("Calle 1", raw)
        self.mod.replace_catalog(
            self.db,
            "employees",
            [{"id": "e9", "name": "Ana", "email": "ana@example.com", "phone_number": "8188888888"}],
        )
        raw = self.db.execute("SELECT raw FROM employees WHERE id = 'e9'").fetchone()[0]
        self.assertIn("Ana", raw)
        self.assertNotIn("ana@example.com", raw)
        self.assertNotIn("8188888888", raw)

    def _sale(self):
        return {
            "receipt_number": "S-1",
            "receipt_type": "SALE",
            "refund_for": None,
            "order": None,
            "created_at": "2026-09-05T01:49:21.000Z",
            "receipt_date": "2026-09-05T01:49:21.000Z",
            "updated_at": "2026-09-05T01:49:21.000Z",
            "cancelled_at": None,
            "source": "point of sale",
            "total_money": 140,
            "total_tax": 0,
            "total_discount": 10,
            "customer_id": "c1",
            "employee_id": "e1",
            "store_id": "s1",
            "dining_option": "Para llevar",
            "note": "sin mayo",
            "tip": 0,
            "surcharge": 0,
            "total_discounts": [{
                "id": "d1",
                "type": "FIXED_PERCENT",
                "name": "Promo",
                "percentage": 10,
                "money_amount": 10,
            }],
            "line_items": [{
                "id": "line1",
                "item_id": "i1",
                "variant_id": "v1",
                "item_name": "Elote Grande",
                "variant_name": None,
                "sku": "SKU1",
                "quantity": 2,
                "price": 70,
                "gross_total_money": 150,
                "total_money": 140,
                "cost": 20,
                "cost_total": 40,
                "total_discount": 10,
                "line_note": None,
                "line_taxes": [],
                "line_discounts": [{
                    "id": "d1",
                    "type": "FIXED_PERCENT",
                    "name": "Promo",
                    "percentage": 10,
                    "money_amount": 10,
                }],
                "line_modifiers": [{
                    "id": "m1",
                    "name": "Toppings",
                    "option": "Chile",
                    "price": 5,
                    "money_amount": 10,
                }],
            }],
            "payments": [{
                "payment_type_id": "p1",
                "name": "Efectivo",
                "type": "CASH",
                "money_amount": 140,
                "paid_at": "2026-09-05T01:49:21.000Z",
                "payment_details": {
                    "authorization_code": "SECRET",
                    "card_number": "•••• 4242",
                },
            }],
        }

    def _refund(self):
        receipt = self._sale()
        receipt.update({
            "receipt_number": "R-1",
            "receipt_type": "REFUND",
            "refund_for": "S-1",
            "receipt_date": "2026-09-05T02:10:00.000Z",
            "created_at": "2026-09-05T02:10:00.000Z",
            "updated_at": "2026-09-05T02:10:00.000Z",
            "total_money": 70,
            "total_discount": 5,
        })
        receipt["line_items"] = [dict(receipt["line_items"][0])]
        receipt["line_items"][0].update({
            "quantity": 1,
            "total_money": 70,
            "total_discount": 5,
            "cost_total": 20,
            "gross_total_money": 75,
        })
        receipt["line_items"][0]["line_discounts"] = [{
            "id": "d1",
            "type": "FIXED_PERCENT",
            "name": "Promo",
            "percentage": 10,
            "money_amount": 5,
        }]
        receipt["payments"] = [{
            "payment_type_id": "p1",
            "name": "Efectivo",
            "type": "CASH",
            "money_amount": 70,
            "paid_at": "2026-09-05T02:10:00.000Z",
        }]
        return receipt

    def _cancelled(self):
        receipt = self._sale()
        receipt.update({
            "receipt_number": "C-1",
            "cancelled_at": "2026-09-05T03:00:00.000Z",
            "total_money": 999,
            "total_discount": 0,
        })
        receipt["line_items"] = [dict(receipt["line_items"][0])]
        receipt["line_items"][0].update({"quantity": 9, "total_money": 999, "total_discount": 0, "cost_total": 0})
        receipt["line_items"][0]["line_discounts"] = []
        receipt["payments"] = [{
            "payment_type_id": "p1",
            "name": "Efectivo",
            "type": "CASH",
            "money_amount": 999,
            "paid_at": "2026-09-05T01:49:21.000Z",
        }]
        return receipt


if __name__ == "__main__":
    unittest.main()
