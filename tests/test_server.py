"""HTTP layer: authentication, server-side RBAC, webhook handling, static hardening, backup/restore, CLI guards."""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import ADMIN
from kffinance import core, db
from kffinance import synthetic as syn
from kffinance.server import Handler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "t.db")
        c = db.connect(cls.path)
        db.init_db(c)
        cls.tok = {n: core.create_user(c, n, r) for n, r in (("admin", "finance_admin"), ("admin2", "finance_admin"), ("approver", "approver"), ("approver2", "approver"), ("preparer", "preparer"), ("viewer", "viewer"))}
        cls.acct = syn.make_accounts(c)["Operating bank"]
        c.close()
        Handler.db_path = cls.path
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, who=None, body=None, headers=None, raw=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        if who:
            h["Authorization"] = "Bearer " + self.tok[who]
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}"), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), e.headers


class TestHTTP(ServerCase):
    def test_static_and_hardening(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as r:
            html = r.read().decode()
            self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("<script>", html.replace('<script src="/app.js"></script>', ""))  # no inline scripts
        self.assertIn("UNAUDITED", html)
        js = open(os.path.join(ROOT, "kffinance/web/app.js")).read()
        self.assertFalse("innerHTML" in js or "outerHTML" in js or "insertAdjacentHTML" in js or "document.write" in js)
        self.assertNotIn("eval(", js)
        for secret_pattern in (r"sk_live_", r"sk_test_", r"whsec_", r"rk_live_"):
            self.assertIsNone(re.search(secret_pattern, js + html))

    def test_authentication_required_everywhere(self):
        self.assertEqual(self.call("GET", "/api/overview")[0], 401)
        self.assertEqual(self.call("GET", "/api/overview", headers={"Authorization": "Bearer nope"})[0], 401)
        self.assertEqual(self.call("POST", "/api/import/csv", body={})[0], 401)
        self.assertEqual(self.call("GET", "/health")[0], 200)

    def test_empty_database_shows_unavailable_not_fabricated_numbers(self):
        s, o, _ = self.call("GET", "/api/overview", "viewer")
        self.assertEqual(s, 200)
        self.assertFalse(o["cash"]["available_operating_cash"]["available"])
        self.assertFalse(o["revenue_this_month"]["net_revenue"]["available"])
        self.assertEqual(o["transactions"]["total"], 0)
        self.assertTrue(o["audit_chain_ok"])
        self.assertIsNone(o["dataset_label"])
        self.assertEqual(o["user"], {"name": "viewer", "role": "viewer"})

    def test_every_view_endpoint_loads_for_a_viewer(self):
        for name in ("overview", "transactions", "revenue", "expenses", "cashflow", "budgets", "accounting", "tax", "planning", "investor", "connectors", "approvals", "flags", "audit"):
            s, body, _ = self.call("GET", f"/api/{name}", "viewer")
            self.assertEqual(s, 200, f"{name}: {body}")
        self.assertEqual(self.call("GET", "/api/nonexistent", "viewer")[0], 404)

    def test_rbac_enforced_server_side(self):
        csv = "date,description,amount,reference\n2026-09-01,Test,-1.00,X1\n"
        body = {"account_id": self.acct, "content": csv, "period": "2026-09"}
        self.assertEqual(self.call("POST", "/api/import/csv", "viewer", body)[0], 403)
        self.assertEqual(self.call("POST", "/api/accounts", "preparer", {"name": "x", "type": "bank", "currency": "SGD"})[0], 403)
        self.assertEqual(self.call("POST", "/api/facts", "preparer", {"key": "gst_registered", "value": "true"})[0], 403)
        self.assertEqual(self.call("POST", "/api/approvals/1/decide", "preparer", {"approve": True})[0], 403)
        self.assertEqual(self.call("POST", "/api/users", "approver", {"action": "create_user", "name": "z", "role": "viewer"})[0], 403)
        self.assertEqual(self.call("POST", "/api/journal/1/post", "preparer", {})[0], 403)

    def test_import_dedup_search_and_injection_safety(self):
        csv = "date,description,amount,reference,counterparty\n2026-09-01,Lovable credits,-50.00,H1,Lovable\n2026-09-02,Supabase,-25.00,H2,Supabase\n"
        body = {"account_id": self.acct, "content": csv, "period": "2026-09", "filename": "h.csv"}
        s, r, _ = self.call("POST", "/api/import/csv", "preparer", body)
        self.assertEqual(s, 200, r)
        imp = next(t for t in r["tasks"] if t["type"] == "import_csv")
        self.assertEqual(imp["result"]["imported"], 2)
        s, r2, _ = self.call("POST", "/api/import/csv", "preparer", body)
        imp2 = next(t for t in r2["tasks"] if t["type"] == "import_csv")
        self.assertEqual(r["workflow"], r2["workflow"])  # same workflow, nothing re-run
        s, t, _ = self.call("GET", "/api/transactions?q=lovable", "viewer")
        self.assertEqual(t["total"], 1)
        row = t["items"][0]
        self.assertEqual(row["amount"], {"available": True, "minor": -5000, "currency": "SGD", "display": "-SGD 50.00"})
        self.assertTrue(row["source_key"].startswith("csv:"))
        s, t, _ = self.call("GET", "/api/transactions?q=%27%20OR%201%3D1%20--", "viewer")
        self.assertEqual((s, t["total"]), (200, 0))
        s, t, _ = self.call("GET", "/api/transactions", "viewer")
        self.assertEqual(t["total"], 2)  # table intact

    def test_approval_flow_over_http_with_separation_of_duties(self):
        s, r, _ = self.call("POST", "/api/invoices", "preparer", {"direction": "payable", "counterparty": "V", "number": "H-1", "issue_date": "2026-09-01", "due_date": "2026-09-30", "amount": "100.00", "currency": "SGD"})
        self.assertEqual(s, 200, r)
        aid = r["approval_id"]
        s, r2, _ = self.call("POST", f"/api/approvals/{aid}/decide", "approver", {"approve": True, "note": "ok"})
        self.assertEqual((s, r2["status"]), (200, "approved"))
        self.assertEqual(self.call("POST", f"/api/approvals/{aid}/decide", "approver2", {"approve": True})[0], 409)  # already decided
        s, r, _ = self.call("POST", "/api/users", "admin", {"action": "create_user", "name": "newbie", "role": "viewer"})
        self.assertEqual(s, 200, r)
        self.assertEqual(self.call("POST", f"/api/approvals/{r['approval_id']}/decide", "admin", {"approve": True})[0], 409)  # own request
        s, r3, _ = self.call("POST", f"/api/approvals/{r['approval_id']}/decide", "admin2", {"approve": True})
        self.assertEqual(s, 200, r3)
        tok = r3["result"]["token_shown_once"]
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/overview", headers={"Authorization": "Bearer " + tok})
        self.assertEqual(urllib.request.urlopen(req).status, 200)

    def test_bad_input_is_rejected_cleanly(self):
        self.assertEqual(self.call("POST", "/api/import/csv", "preparer", raw=b"{not json")[0], 400)
        s, r, _ = self.call("POST", "/api/budgets", "preparer", {"period": "2026-09", "department": "ops", "category": "6100", "amount": "12.345"})
        self.assertEqual(s, 400)
        self.assertNotIn("Traceback", json.dumps(r))
        self.assertEqual(self.call("POST", "/api/payments/authorise", "preparer", {"invoice_id": 99999})[0], 409)

    def test_snapshot_verification_requires_an_approver(self):
        b = {"account_id": self.acct, "as_of": "2026-09-30", "amount": "100.00", "currency": "SGD", "source": "statement", "evidence_ref": "stmt-sep.pdf"}
        s, r, _ = self.call("POST", "/api/snapshots", "preparer", b)
        self.assertEqual((s, r["verified"]), (200, False))
        s, o, _ = self.call("GET", "/api/cashflow", "viewer")
        self.assertFalse(o["cash"]["available_operating_cash"]["available"])  # unverified is not a balance
        b["as_of"] = "2026-09-29"
        s, r, _ = self.call("POST", "/api/snapshots", "approver", b)
        self.assertTrue(r["verified"])
        s, o, _ = self.call("GET", "/api/cashflow", "viewer")
        self.assertFalse(o["cash"]["available_operating_cash"]["available"])  # the other operating account still has no verified balance
        bank = next(a for a in o["cash"]["accounts"] if a["account_id"] == self.acct)
        self.assertEqual(bank["balance"]["minor"], 10000)
        other = next(a for a in o["cash"]["accounts"] if a["account_id"] != self.acct and a["funds_class"] == "operating")
        self.call("POST", "/api/snapshots", "approver", {**b, "account_id": other["account_id"], "as_of": "2026-09-29", "amount": "5.00"})
        s, o, _ = self.call("GET", "/api/cashflow", "viewer")
        self.assertEqual(o["cash"]["available_operating_cash"]["minor"], 10500)
        self.assertEqual(self.call("POST", "/api/snapshots", "approver", {**b, "as_of": "2026-09-28", "evidence_ref": ""})[0], 409)


class TestWebhookEndpoint(ServerCase):
    def sign(self, body, secret, ts=None):
        ts = ts or int(time.time())
        return f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()

    def test_rejected_without_secret_and_bad_signature_accepted_when_valid_idempotent(self):
        body = json.dumps({"id": "evt_http", "type": "payment_intent.succeeded", "data": {"object": {"id": "pi_x", "amount": 100, "currency": "sgd"}}}).encode()
        os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
        self.assertEqual(self.call("POST", "/webhooks/stripe", raw=body)[0], 503)
        os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_unit"
        self.addCleanup(os.environ.pop, "STRIPE_WEBHOOK_SECRET", None)
        self.assertEqual(self.call("POST", "/webhooks/stripe", raw=body, headers={"Stripe-Signature": "t=1,v1=bad"})[0], 400)
        sig = self.sign(body, "whsec_unit")
        s, r, _ = self.call("POST", "/webhooks/stripe", raw=body, headers={"Stripe-Signature": sig})
        self.assertEqual((s, r["recorded"]), (200, True))
        s, r, _ = self.call("POST", "/webhooks/stripe", raw=body, headers={"Stripe-Signature": sig})
        self.assertEqual((s, r["duplicate"]), (200, True))
        c = db.connect(self.path)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM payment_events WHERE event_id='evt_http'").fetchone()[0], 1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM transactions WHERE source_id IN (SELECT id FROM source_records WHERE source_key LIKE '%evt_http%')").fetchone()[0], 0)
        c.close()


class TestBackupAndCLI(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "kffinance", *args], cwd=ROOT, capture_output=True, text=True)

    def test_backup_restore_roundtrip_preserves_data_and_audit_chain(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, "a.db"), os.path.join(d, "b.db")
            r = self.run_cli("seed-synthetic", "--db", src)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = self.run_cli("backup", "--db", src, "--out", dst)
            self.assertEqual(r.returncode, 0, r.stderr)
            a, b = sqlite3.connect(src), sqlite3.connect(dst)
            for t in ("transactions", "source_records", "audit_log", "invoices"):
                self.assertEqual(a.execute(f"SELECT COUNT(*) FROM {t}").fetchone(), b.execute(f"SELECT COUNT(*) FROM {t}").fetchone())
            self.assertEqual(b.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(self.run_cli("verify-audit", "--db", dst).returncode, 0)
            a.close(); b.close()

    def test_synthetic_seed_refuses_the_default_database_path(self):
        r = self.run_cli("seed-synthetic")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Refusing", r.stderr + r.stdout)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "data", "kffinance.db")))

    def test_init_creates_admin_once(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.db")
            r1 = self.run_cli("init", "--db", p)
            self.assertIn("kff_", r1.stdout)
            r2 = self.run_cli("init", "--db", p)
            self.assertNotIn("kff_", r2.stdout)

    def test_audit_tamper_detected_by_cli(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.db")
            self.run_cli("seed-synthetic", "--db", p)
            c = sqlite3.connect(p)
            c.execute("DROP TRIGGER audit_no_update")
            c.execute("UPDATE audit_log SET actor='mallory' WHERE id=3")
            c.commit(); c.close()
            r = self.run_cli("verify-audit", "--db", p)
            self.assertEqual(r.returncode, 1)
            self.assertIn("BROKEN", r.stdout)


if __name__ == "__main__":
    unittest.main()


class TestAmendHTTP(ServerCase):
    def test_rules_coa_and_settings_over_http(self):
        self.assertEqual(self.call("POST", "/api/rules", "preparer", {"pattern": "notion", "coa_code": "6100"})[0], 403)
        s, r, _ = self.call("POST", "/api/rules", "approver", {"pattern": " Notion ", "coa_code": "6100", "confidence": 0.9})
        self.assertEqual(s, 200, r)
        self.assertEqual(self.call("POST", "/api/rules", "approver", {"pattern": "x", "coa_code": "9999"})[0], 409)
        s, acc, _ = self.call("GET", "/api/accounting", "viewer")
        self.assertIn("notion", [x["pattern"] for x in acc["category_rules"]])
        self.assertEqual(self.call("POST", "/api/coa", "approver", {"code": "6950", "name": "Test", "type": "expense"})[0], 403)
        self.assertEqual(self.call("POST", "/api/coa", "admin", {"code": "6950", "name": "Test", "type": "expense"})[0], 200)
        self.assertEqual(self.call("POST", "/api/coa", "admin", {"code": "6950", "name": "Dup", "type": "expense"})[0], 409)
        self.assertEqual(self.call("POST", "/api/coa", "admin", {"code": "69", "name": "Bad", "type": "expense"})[0], 409)
        self.assertEqual(self.call("POST", "/api/settings", "approver", {"key": "approval_threshold_minor", "value": "1"})[0], 403)
        self.assertEqual(self.call("POST", "/api/settings", "admin", {"key": "base_currency", "value": "USD"})[0], 409)
        s, r, _ = self.call("POST", "/api/settings", "admin", {"key": "ar_collection_delay_days", "value": "14"})
        self.assertEqual(s, 200, r)
        s, r2, _ = self.call("POST", f"/api/approvals/{r['approval_id']}/decide", "admin2", {"approve": True})
        self.assertEqual((s, r2["result"]["new"]), (200, "14"))
