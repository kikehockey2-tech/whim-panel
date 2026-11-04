#!/usr/bin/env python3
"""Pruebas del sync sin llamar a Loyverse y sin un token real."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_loyverse as sync


class ResolveTests(unittest.TestCase):
    def test_accent_and_mercadito_suffix_map_to_existing_ids(self):
        self.assertEqual(sync.resolve_product_id("Frappé Café"), "frappe_cafe")
        self.assertEqual(sync.resolve_product_id("Frappe Cafe"), "frappe_cafe")
        self.assertEqual(sync.resolve_product_id("Frappe Mocha"), "frappe_mocha")
        self.assertEqual(sync.resolve_product_id("Elote Grande M"), "elote_grande")
        self.assertEqual(sync.resolve_product_id("Elote Mediano M"), "elote_mediano")
        self.assertEqual(sync.resolve_product_id("Whimix Bowl M"), "whimix_bowl")
        self.assertEqual(sync.resolve_product_id("Smoothie Mangonada M"), "smoothie_mangonada")
        self.assertEqual(sync.resolve_product_id("Bowl Jicam dh"), "bowl_jicama")

    def test_unknown_names_stay_unmapped(self):
        self.assertIsNone(sync.resolve_product_id("Suerito M"))
        self.assertIsNone(sync.resolve_product_id("Smoothie Fresa M"))

    def test_same_day_aliases_add_up(self):
        now = datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)
        receipts = [{
            "receipt_type": "SALE",
            "receipt_date": "2026-11-04T18:00:00.000Z",
            "cancelled_at": None,
            "line_items": [
                {"item_name": "Elote Grande", "quantity": 1, "total_money": 70},
                {"item_name": "Elote Grande M", "quantity": 2, "total_money": 160},
                {"item_name": "Suerito M", "quantity": 4, "total_money": 400},
            ],
        }]
        out, unmapped = sync.build_payload(receipts, {}, now, {})
        self.assertEqual(out["todaySales"]["elote_grande"], 3)
        self.assertNotIn("suerito", out["todaySales"])
        self.assertEqual(unmapped, {"Suerito M"})
        self.assertNotIn("secret-token", json.dumps(out))


class HistoryTests(unittest.TestCase):
    def test_older_days_survive_a_shorter_window(self):
        fresh = {"2026-11-03": {"kind": "local", "total": 10, "items": []}}
        previous = {
            "2026-06-08": {"kind": "local", "total": 70, "items": []},
            "2026-11-03": {"kind": "local", "total": 1, "items": []},
        }
        merged = sync.merge_daily_sales(fresh, previous)
        self.assertEqual(merged["2026-06-08"]["total"], 70)
        self.assertEqual(merged["2026-11-03"]["total"], 10)

    def test_payload_keeps_previous_days_and_previous_costs(self):
        now = datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)
        previous = {
            "dailySales": {"2026-06-08": {"kind": "local", "total": 70, "items": []}},
            "productCost": {"elote_grande": 22.0},
        }
        out, _ = sync.build_payload([], {}, now, previous)
        self.assertEqual(out["dailySales"]["2026-06-08"]["total"], 70)
        self.assertEqual(out["productCost"]["elote_grande"], 22.0)
        self.assertEqual(out["trend"][-1], 0)


class FetchTests(unittest.TestCase):
    def test_402_retries_short_window(self):
        calls = []

        def fake(token, since, until):
            calls.append((until - since).days)
            if len(calls) == 1:
                raise sync.LoyverseHTTPError(402, '{"code":"PAYMENT_REQUIRED"}')
            return [{"id": "r1"}]

        orig = sync.fetch_all_receipts
        sync.fetch_all_receipts = fake
        try:
            receipts, days = sync.fetch_receipts("not-a-real-token", datetime(2026, 11, 4, tzinfo=timezone.utc))
        finally:
            sync.fetch_all_receipts = orig
        self.assertEqual(receipts, [{"id": "r1"}])
        self.assertEqual(days, sync.FREE_HISTORY_DAYS)
        self.assertGreater(calls[0], 31)
        self.assertLessEqual(calls[1], 30)

    def test_401_says_token_is_invalid_and_does_not_write(self):
        def fake(token, since, until):
            raise sync.LoyverseHTTPError(401, "invalid token not-a-real-token")

        orig = sync.fetch_all_receipts
        sync.fetch_all_receipts = fake
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "data.json")
                os.environ["LOYVERSE_TOKEN"] = "not-a-real-token"
                os.environ["WHIM_DATA_PATH"] = path
                try:
                    code = sync.main()
                finally:
                    os.environ.pop("LOYVERSE_TOKEN", None)
                    os.environ.pop("WHIM_DATA_PATH", None)
                self.assertEqual(code, 1)
                self.assertFalse(os.path.exists(path))
        finally:
            sync.fetch_all_receipts = orig

    def test_missing_token_exits(self):
        os.environ.pop("LOYVERSE_TOKEN", None)
        self.assertEqual(sync.main(), 1)

    def test_redact_strips_token(self):
        self.assertNotIn("abc123", sync.redact("Bearer abc123 denied", "abc123"))
        self.assertIn("[REDACTED]", sync.redact("Bearer abc123 denied", "abc123"))


if __name__ == "__main__":
    unittest.main()
