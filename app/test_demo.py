"""Synthetic fixtures only. Every database lives in an isolated temporary folder."""
import copy
import csv
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from backend import APP_DIR, AppError, DEFAULT_SEED, Store, planning_recommendation, recommend_row
from server import LocalServer, make_handler

ROOT = APP_DIR.parent
STYLE = "DEMO-ALPHA"
SKU = "DEMO-ALPHA-Blue-6"
SKU2 = "DEMO-ALPHA-Blue-7"


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "isolated.sqlite3", DEFAULT_SEED, actor="synthetic-test")
        self.document = json.loads(DEFAULT_SEED.read_text(encoding="utf-8"))

    def plan(self, quantities):
        plan = self.store.create_plan({"style": STYLE})
        return self.store.update_plan(plan["id"], {"revision": plan["revision"], "lines": [
            {"sku": sku, "quantity": quantity} for sku, quantity in quantities.items()]})

    def buy(self, quantities):
        plan = self.plan(quantities)
        return self.store.confirm_plan(plan["id"], {"revision": plan["revision"], "confirmed": True,
                                                  "reference": "虚构测试采购"})

    def test_seed_is_runnable_and_import_is_idempotent(self):
        state = self.store.bootstrap()
        self.assertEqual(len(state["rows"]), 8)
        self.assertEqual({row["style"] for row in state["rows"]}, {"DEMO-ALPHA", "DEMO-BETA"})
        self.assertFalse(self.store.import_snapshot(self.document)["imported"])
        incomplete = copy.deepcopy(self.document)
        incomplete["rows"].pop()
        with self.assertRaises(AppError):
            self.store.import_snapshot(incomplete)
        self.assertEqual(len(self.store.bootstrap()["rows"]), 8)

    def test_manual_zero_survives_adoption_import_and_restart(self):
        plan = self.plan({SKU: 0})
        adopted = self.store.adopt_plan(plan["id"], {"revision": plan["revision"]})
        self.assertEqual(next(line for line in adopted["lines"] if line["sku"] == SKU)["quantity"], 0)
        newer = copy.deepcopy(self.document)
        newer["salesEnd"] = "2020-02-15"
        self.store.import_snapshot(newer)
        reopened = Store(self.store.path, DEFAULT_SEED)
        saved = reopened.bootstrap()["plans"][0]
        self.assertEqual(saved, adopted)
        self.assertTrue(next(line for line in saved["lines"] if line["sku"] == SKU)["manual"])

    def test_stale_revision_does_not_overwrite_user_quantities(self):
        plan = self.plan({SKU: 7})
        with self.assertRaises(AppError) as failure:
            self.store.update_plan(plan["id"], {"revision": plan["revision"] - 1,
                                              "lines": [{"sku": SKU, "quantity": 99}]})
        self.assertEqual(failure.exception.code, "revision_conflict")
        self.assertEqual(self.store.bootstrap()["plans"][0], plan)

    def test_purchase_requires_confirmation_and_cancellation_preserves_stock(self):
        original = self.store.bootstrap()["rows"]
        plan = self.plan({SKU: 7, SKU2: 0})
        with self.assertRaises(AppError):
            self.store.confirm_plan(plan["id"], {"revision": plan["revision"], "reference": "演示"})
        self.assertEqual(self.store.bootstrap()["purchases"], [])
        payload = {"revision": plan["revision"], "confirmed": True, "reference": "演示"}
        purchase = self.store.confirm_plan(plan["id"], payload)
        self.assertEqual(self.store.confirm_plan(plan["id"], payload)["id"], purchase["id"])
        self.assertEqual(len(purchase["lines"]), 1)
        self.assertEqual(self.store.recommend(STYLE)["evaluated"][0]["result"]["status"], "supply")
        cancelled = self.store.cancel_purchase(purchase["id"], {"revision": purchase["revision"], "confirmed": True})
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["lines"], purchase["lines"])
        self.assertEqual(self.store.bootstrap()["rows"], original)
        self.assertEqual(self.store.bootstrap()["plans"][0]["status"], "cancelled")

    def test_fabricated_tier_costs_and_unknown_price(self):
        plan = self.plan({SKU: 3, SKU2: 4})
        rows = list(csv.DictReader(io.StringIO(self.store.export_csv(plan["id"]).decode("utf-8-sig"))))
        self.assertTrue(all(row["预计货款人民币"] == "待核算" for row in rows))
        self.store.update_style_preference(STYLE, {"revision": 0, "purchase_unit_cny": None,
            "purchase_price_tiers": [{"cn_min": 36, "cn_max": 36, "unit_cny": 12.34},
                                     {"cn_min": 37, "cn_max": 37, "unit_cny": 23.45}],
            "purchase_price_includes_domestic_freight": True, "purchase_price_note": "完全虚构测试报价"})
        rows = list(csv.DictReader(io.StringIO(self.store.export_csv(plan["id"]).decode("utf-8-sig"))))
        self.assertEqual([row["预计货款人民币"] for row in rows], ["37.02", "93.80"])
        self.assertEqual(self.store.bootstrap()["purchases"], [])

    def test_generic_packing_unknown_and_first_launch_rules(self):
        row = self.document["rows"][0]
        self.assertEqual(planning_recommendation(row)["quantity"] % 2, 0)
        changed = dict(row, orderMultiple=3)
        self.assertEqual(planning_recommendation(changed)["quantity"] % 3, 0)
        self.assertEqual(planning_recommendation(self.document["rows"][5])["status"], "defer")
        self.assertEqual(planning_recommendation(self.document["rows"][6])["status"], "planned")
        self.assertEqual(planning_recommendation(self.document["rows"][7])["status"], "fbm_only")
        bad = copy.deepcopy(self.document)
        bad["rows"][0]["orderMultiple"] = True
        with self.assertRaises(AppError):
            self.store.import_snapshot(bad)

    def test_configured_shipment_units_cannot_be_allocated_twice(self):
        first, second = self.buy({SKU: 3}), self.buy({SKU: 2})
        imports = self.root / "imports"
        imports.mkdir()
        proof = {"source": "lingxing", "sid": "DEMO-SHOP", "dispatch_confirmed": True,
                 "shipment_id": "DEMO-SHIPMENT", "observed_at": "2020-02-15T01:00:00+00:00",
                 "ship_date": None, "lines": [{"sku": SKU, "quantity": 3}]}
        (imports / "proof.json").write_text(json.dumps(proof), encoding="utf-8")
        def payload(purchase):
            return {"revision": purchase["revision"], "match_id": "demo-match",
                    "source_file": "imports/proof.json", "mapping_note": "虚构逐批对应",
                    "lines": [{"sku": SKU, "quantity": purchase["lines"][0]["ordered"]}]}
        with patch.dict(os.environ, {"REPLENISHMENT_LINGXING_SID": ""}):
            with self.assertRaises(AppError):
                self.store.match_lingxing_shipment(first["id"], payload(first))
        with patch.dict(os.environ, {"REPLENISHMENT_LINGXING_SID": "DEMO-SHOP"}):
            matched = self.store.match_lingxing_shipment(first["id"], payload(first))
            self.assertIn("shipment_archived_at", matched)
            self.assertEqual(matched["lines"][0]["received"], 0)
            self.store.cancel_purchase(matched["id"], {"revision": matched["revision"], "confirmed": True})
            with self.assertRaises(AppError):
                self.store.match_lingxing_shipment(second["id"], payload(second))
        self.assertEqual(self.store.bootstrap()["rows"], self.document["rows"])

    def test_http_page_data_and_write_protection(self):
        server = LocalServer(("127.0.0.1", 0), make_handler(self.store, "demo-test-token", APP_DIR / "static"))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:" + str(server.server_address[1])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for path in ("/", "/app.js", "/styles.css", "/api/bootstrap", "/api/recommendations"):
            with self.subTest(path=path), opener.open(base + path, timeout=5) as response:
                self.assertEqual(response.status, 200)
        body = json.dumps({"style": STYLE}).encode()
        request = urllib.request.Request(base + "/api/plans", data=body, headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as failure:
            opener.open(request, timeout=5)
        self.assertEqual(failure.exception.code, 403)
        self.assertEqual(self.store.bootstrap()["plans"], [])

    @unittest.skipUnless(shutil.which("node"), "Node.js 未安装，跳过独立 JavaScript 规则核对")
    def test_javascript_and_python_rules_match(self):
        code = ("const fs=require('node:fs');const rules=require(process.argv[1]);"
                "const data=JSON.parse(fs.readFileSync(0,'utf8'));"
                "console.log(JSON.stringify([21,35,49].map(days=>data.rows.map(row=>rules.recommend(row,days)))));")
        completed = subprocess.run(["node", "-e", code, str(ROOT / "recommendation-rules.js")],
            input=json.dumps(self.document, ensure_ascii=False), text=True, encoding="utf-8", capture_output=True, check=True)
        fields = ("quantity", "gap", "target", "daily", "lowSample", "supplies", "status", "label", "reason")
        for days, expected_results in zip((21, 35, 49), json.loads(completed.stdout)):
            for row, expected in zip(self.document["rows"], expected_results):
                actual = recommend_row(row, days)
                with self.subTest(sku=row["sku"], days=days):
                    for field in fields:
                        self.assertEqual(actual.get(field), expected.get(field), field)


if __name__ == "__main__":
    unittest.main()
