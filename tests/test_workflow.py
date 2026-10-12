"""Ingestion, duplicate prevention, categorisation, matching, reconciliation and the orchestrator."""
import hashlib
import hmac
import json
import time
import unittest

from helpers import ADMIN, APPROVER, APPROVER2, PREPARER, VIEWER, Base
from kffinance import core
from kffinance import orchestrator as orch
from kffinance import synthetic as syn
from kffinance.agents import bookkeeping as bk
from kffinance.core import ControlViolation, Forbidden
from kffinance.ingest import (extract_document_fields, import_csv, import_stripe_balance_transactions, record_payment_event, verify_stripe_signature)

CSV = "date,description,amount,reference\n2026-09-01,Lovable credits,-50.00,R1\n2026-09-02,Supabase,-25.00,R2\n"


class TestImport(Base):
    def setUp(self):
        super().setUp()
        self.a = self.acct()

    def test_same_file_twice_changes_nothing(self):
        r1 = import_csv(self.c, PREPARER, self.a, CSV)
        r2 = import_csv(self.c, PREPARER, self.a, CSV)
        self.assertEqual(r1["imported"], 2)
        self.assertEqual(r2["status"], "duplicate_file")
        self.assertEqual(self.one("SELECT COUNT(*) FROM transactions")[0], 2)
        self.assertEqual(self.one("SELECT COUNT(*) FROM source_records")[0], 2)

    def test_overlapping_file_skips_known_source_refs(self):
        import_csv(self.c, PREPARER, self.a, CSV)
        r = import_csv(self.c, PREPARER, self.a, CSV + "2026-09-03,Github,-10.00,R3\n")
        self.assertEqual((r["imported"], r["duplicate_source"]), (1, 2))
        self.assertEqual(self.one("SELECT COUNT(*) FROM transactions")[0], 3)

    def test_rows_without_reference_get_stable_keys_and_identical_rows_stay_distinct(self):
        csv_ = "date,description,amount\n2026-09-01,Coffee,-5.00\n2026-09-01,Coffee,-5.00\n"
        self.assertEqual(import_csv(self.c, PREPARER, self.a, csv_)["imported"], 2)
        r = import_csv(self.c, PREPARER, self.a, csv_ + "2026-09-02,Tea,-3.00\n")
        self.assertEqual((r["imported"], r["duplicate_source"]), (1, 2))

    def test_bad_rows_are_recorded_not_silently_dropped(self):
        bad = ("date,description,amount,currency\n2026-09-01,ok,-1.00,SGD\nnot-a-date,x,-1.00,SGD\n2026-09-01,x,1.005,SGD\n"
               "2026-09-01,usd row,-1.00,USD\n2026-09-01,zero,0,SGD\n")
        r = import_csv(self.c, PREPARER, self.a, bad)
        self.assertEqual((r["imported"], r["rejected"], r["total"]), (1, 4, 5))
        reasons = " | ".join(x["reason"] for x in self.c.execute("SELECT reason FROM import_rejects"))
        for frag in ("unrecognised date", "decimal places", "differs from account currency", "zero amount"):
            self.assertIn(frag, reasons)

    def test_missing_columns_roll_back_everything(self):
        with self.assertRaises(ControlViolation):
            import_csv(self.c, PREPARER, self.a, "foo,bar\n1,2\n")
        self.assertEqual(self.one("SELECT COUNT(*) FROM import_batches")[0], 0)

    def test_viewer_cannot_import_and_every_txn_has_a_source(self):
        with self.assertRaises(Forbidden):
            import_csv(self.c, VIEWER, self.a, CSV)
        import_csv(self.c, PREPARER, self.a, CSV)
        self.assertEqual(self.one("SELECT COUNT(*) FROM transactions t LEFT JOIN source_records s ON s.id=t.source_id WHERE s.id IS NULL")[0], 0)

    def test_source_fields_cannot_be_edited_or_deleted(self):
        import_csv(self.c, PREPARER, self.a, CSV)
        import sqlite3
        for sql in ("UPDATE transactions SET amount_minor=1", "UPDATE transactions SET currency='USD'", "DELETE FROM transactions", "UPDATE source_records SET raw_json='{}'"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.c.execute(sql)


class TestStripe(Base):
    NOW = 2_000_000_000

    def bts(self):
        return [{"id": "txn_1", "type": "charge", "amount": 2200, "fee": 94, "currency": "sgd", "created": self.NOW - 86400 * 5, "available_on": self.NOW - 86400 * 3, "source": "ch_1", "description": "Platform fee"},
                {"id": "txn_2", "type": "charge", "amount": 1900, "fee": 85, "currency": "sgd", "created": self.NOW - 86400, "available_on": self.NOW + 86400, "source": "ch_2"},
                {"id": "txn_3", "type": "refund", "amount": -1900, "fee": 0, "currency": "sgd", "created": self.NOW - 3600, "available_on": self.NOW - 3600, "source": "re_1"},
                {"id": "txn_4", "type": "payout", "amount": -1500, "fee": 0, "currency": "sgd", "created": self.NOW - 3600, "available_on": self.NOW - 3600}]

    def test_import_splits_fees_and_marks_unsettled(self):
        a = self.acct("Stripe", typ="payment_processor")
        r = import_stripe_balance_transactions(self.c, PREPARER, a, self.bts(), now_epoch=self.NOW)
        self.assertEqual(r["imported"], 6)  # 4 balance txns + 2 fee lines
        fees = self.one("SELECT COUNT(*), SUM(amount_minor) FROM transactions WHERE kind='fee'")
        self.assertEqual((fees[0], fees[1]), (2, -179))
        self.assertEqual(self.one("SELECT status FROM transactions WHERE source_id=(SELECT id FROM source_records WHERE source_key LIKE '%txn_2')")[0], "pending")
        self.assertEqual(self.one("SELECT kind FROM transactions WHERE source_id=(SELECT id FROM source_records WHERE source_key LIKE '%txn_4')")[0], "transfer")
        again = import_stripe_balance_transactions(self.c, PREPARER, a, self.bts(), now_epoch=self.NOW)
        self.assertEqual(again["status"], "duplicate_file")

    def test_replaying_a_subset_does_not_double_count(self):
        a = self.acct("Stripe", typ="payment_processor")
        import_stripe_balance_transactions(self.c, PREPARER, a, self.bts(), now_epoch=self.NOW)
        r = import_stripe_balance_transactions(self.c, PREPARER, a, self.bts()[:2], now_epoch=self.NOW)
        self.assertEqual(r["imported"], 0)
        self.assertEqual(r["duplicate_source"], 2)
        self.assertEqual(self.one("SELECT COUNT(*) FROM transactions")[0], 6)

    def test_webhook_signature(self):
        secret, body = "whsec_test", b'{"id":"evt_1"}'
        ts = int(time.time())
        good = f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_stripe_signature(body, good, secret))
        self.assertFalse(verify_stripe_signature(body + b" ", good, secret))
        self.assertFalse(verify_stripe_signature(body, good, "other"))
        self.assertFalse(verify_stripe_signature(body, "garbage", secret))
        old = f"t={ts - 4000},v1=" + hmac.new(secret.encode(), f"{ts - 4000}.".encode() + body, hashlib.sha256).hexdigest()
        self.assertFalse(verify_stripe_signature(body, old, secret))

    def test_event_is_not_settlement_or_revenue_until_balance_txn_arrives(self):
        a = self.acct("Stripe", typ="payment_processor")
        ev = {"id": "evt_1", "type": "payment_intent.succeeded", "livemode": False, "data": {"object": {"id": "ch_1", "amount": 2200, "currency": "sgd"}}}
        with self.assertRaises(ControlViolation):
            record_payment_event(self.c, ev, False)
        self.assertTrue(record_payment_event(self.c, ev, True)["recorded"])
        self.assertTrue(record_payment_event(self.c, ev, True)["duplicate"])  # redelivery
        self.assertEqual(self.one("SELECT COUNT(*) FROM transactions")[0], 0)
        self.assertEqual(self.one("SELECT settlement_status FROM payment_events")[0], "unsettled")
        import_stripe_balance_transactions(self.c, PREPARER, a, self.bts(), now_epoch=self.NOW)
        self.assertEqual(self.one("SELECT settlement_status FROM payment_events")[0], "settled")


class TestBookkeepingAgent(Base):
    def setUp(self):
        super().setUp()
        d = syn.load_basic(self.c, ADMIN, PREPARER)
        self.bank = d["accounts"]["Operating bank"]
        self.r = import_csv(self.c, PREPARER, self.bank, syn.BANK_CSV)

    def txn(self, ref):
        return self.one("SELECT t.* FROM transactions t JOIN source_records s ON s.id=t.source_id WHERE s.source_key=?", f"csv:{self.bank}:{ref}")

    def test_import_counts(self):
        self.assertEqual((self.r["imported"], self.r["rejected"]), (14, 1))

    def test_categorise_proposes_never_self_approves(self):
        out = bk.categorise(self.c, PREPARER)
        self.assertGreater(out["proposed"], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM transactions WHERE classification_status='approved'")[0], 0)
        t = self.txn("B004")
        self.assertEqual((t["category"], t["classification_status"]), ("5100", "proposed"))
        unk = self.txn("B014")
        self.assertEqual(unk["classification_status"], "uncategorised")
        self.assertTrue(self.one("SELECT 1 FROM flags WHERE entity_id=? AND code='needs_classification'", str(unk["record_id"])))

    def test_classification_threshold_needs_approver(self):
        bk.categorise(self.c, PREPARER)
        big = self.txn("B010")  # SGD 1,200 >= threshold
        with self.assertRaisesRegex(ControlViolation, "approver"):
            bk.classify_transaction(self.c, PREPARER, big["record_id"], "6300")
        bk.classify_transaction(self.c, APPROVER, big["record_id"], "6300")
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):  # approved classification is locked
            self.c.execute("UPDATE transactions SET category='6900' WHERE record_id=?", (big["record_id"],))
        with self.assertRaises(ControlViolation):
            bk.classify_transaction(self.c, APPROVER, big["record_id"], "6900")
        with self.assertRaises(ControlViolation):
            bk.classify_transaction(self.c, APPROVER, self.txn("B004")["record_id"], "9999")

    def test_duplicate_detection(self):
        bk.detect_duplicates(self.c, PREPARER)
        dup = self.txn("B011")
        self.assertTrue(self.one("SELECT 1 FROM flags WHERE entity_id=? AND code='possible_duplicate'", str(dup["record_id"])))
        self.assertFalse(self.one("SELECT 1 FROM flags WHERE entity_id=? AND code='possible_duplicate'", str(self.txn("B010")["record_id"])))

    def test_matching_and_discrepancies(self):
        out = bk.match_transactions(self.c, PREPARER)
        tan, lim = self.txn("B012"), self.txn("B013")
        tan, lim = self.one("SELECT * FROM transactions WHERE record_id=?", tan["record_id"]), self.one("SELECT * FROM transactions WHERE record_id=?", lim["record_id"])
        self.assertEqual(tan["reconciliation_status"], "matched")
        self.assertEqual(lim["reconciliation_status"], "exception")  # 218.00 vs invoice 220.00
        self.assertTrue(self.one("SELECT 1 FROM flags WHERE entity_id=? AND code='amount_discrepancy' AND severity='material'", str(lim["record_id"])))
        self.assertEqual(self.one("SELECT reconciliation_status FROM transactions WHERE record_id=?", self.txn("B009")["record_id"])[0], "matched")  # Lovable receipt
        self.assertGreaterEqual(out["confirmed"], 2)

    def test_material_flag_needs_approver_and_note(self):
        bk.match_transactions(self.c, PREPARER)
        f = self.one("SELECT id FROM flags WHERE code='amount_discrepancy'")[0]
        with self.assertRaises(ControlViolation):
            bk.resolve_flag(self.c, PREPARER, f, "looked fine")
        with self.assertRaisesRegex(ControlViolation, "note"):
            bk.resolve_flag(self.c, APPROVER, f, "  ")
        bk.resolve_flag(self.c, APPROVER, f, "customer short-paid, credit note issued")
        self.assertEqual(self.one("SELECT status FROM flags WHERE id=?", f)[0], "resolved")

    def test_missing_receipt_and_unusual_spend_flags(self):
        bk.match_transactions(self.c, PREPARER)
        bk.scan_flags(self.c, PREPARER)
        self.assertTrue(self.one("SELECT 1 FROM flags WHERE entity_id=? AND code='missing_receipt'", str(self.txn("B003")["record_id"])))  # payroll, no document
        self.assertFalse(self.one("SELECT 1 FROM flags WHERE entity_id=? AND code='missing_receipt'", str(self.txn("B009")["record_id"])))  # has receipt

    def test_journal_proposals_do_not_recognise_revenue_from_cash(self):
        bk.categorise(self.c, PREPARER)
        for ref in ("B012", "B014"):
            bk.classify_transaction(self.c, PREPARER, self.txn(ref)["record_id"], "4000")
        bk.match_transactions(self.c, PREPARER)
        bk.propose_entries(self.c, PREPARER)
        def accts(ref):
            e = self.one("SELECT id FROM journal_entries WHERE source_ref=?", f"txn:{self.txn(ref)['record_id']}")[0]
            return {r["account_code"] for r in self.c.execute("SELECT account_code FROM journal_lines WHERE entry_id=?", (e,))}
        self.assertEqual(accts("B012"), {"1000", "1100"})  # settles receivable
        self.assertEqual(accts("B014"), {"1000", "2400"})  # suspense, not revenue
        self.assertEqual(self.one("SELECT COUNT(*) FROM journal_lines WHERE account_code IN ('4000','4100')")[0], 0)

    def test_tax_on_invoice_without_verified_gst_status_is_blocked(self):
        from kffinance.agents import payables
        r = payables.submit_invoice(self.c, PREPARER, "receivable", "GST Co", "G1", "2026-09-02", None, 10000, 900, "SGD")
        core.decide_approval(self.c, APPROVER, r["approval_id"], True)
        bk.propose_invoice_entries(self.c, PREPARER)
        self.assertFalse(self.one("SELECT 1 FROM journal_entries WHERE source_ref=?", f"inv:{r['invoice_id']}"))
        self.assertTrue(self.one("SELECT 1 FROM flags WHERE code='tax_without_verified_registration'"))

    def test_reconciliation_is_incomplete_without_evidence_and_complete_with_it(self):
        rep = bk.reconciliation_report(self.c, PREPARER, self.bank, "2026-09")
        self.assertEqual(rep["status"], "incomplete")
        self.assertIn("no verified statement closing balance", rep["reasons"])
        # a clean second account that genuinely reconciles
        a2 = self.acct("Clean", "SGD")
        self.snapshot(a2, "2026-08-31", 100000)
        from kffinance.agents import payables
        payables.submit_invoice(self.c, PREPARER, "receivable", "Zeta Holdings", "Z-1", "2026-09-01", "2026-09-15", 1900, 0, "SGD", product="platform fee")
        import_csv(self.c, PREPARER, a2, "date,description,amount,reference,counterparty\n2026-09-10,Zeta Holdings fee,19.00,C1,Zeta Holdings\n")
        bk.categorise(self.c, PREPARER)
        t = self.one("SELECT record_id FROM transactions WHERE account_id=?", a2)[0]
        bk.classify_transaction(self.c, PREPARER, t, "4000")
        bk.match_transactions(self.c, PREPARER)
        self.assertEqual(bk.reconciliation_report(self.c, PREPARER, a2, "2026-09")["status"], "incomplete")  # still no closing evidence
        self.snapshot(a2, "2026-09-30", 100000 + 1900)
        ok = bk.reconciliation_report(self.c, PREPARER, a2, "2026-09")
        self.assertEqual((ok["status"], ok["difference_minor"]), ("complete", 0), ok["reasons"])
        # a wrong closing balance must surface a difference, never be hidden
        a3 = self.acct("Off", "SGD")
        self.snapshot(a3, "2026-08-31", 1000)
        self.snapshot(a3, "2026-09-30", 5000)
        bad = bk.reconciliation_report(self.c, PREPARER, a3, "2026-09")
        self.assertEqual((bad["status"], bad["difference_minor"]), ("incomplete", 4000))

    def test_document_extraction_never_guesses(self):
        f = extract_document_fields("Acme Pte Ltd\nInvoice date 2026-09-01\nTotal SGD 109.00\nGST 9.00\nNet 30")
        self.assertEqual((f["vendor"], f["amount"], f["tax"], f["date"], f["complete"]), ("Acme Pte Ltd", 10900, 900, "2026-09-01", True))
        g = extract_document_fields("Mystery vendor\nsome text")
        self.assertFalse(g["complete"])
        self.assertIsNone(g["amount"])


class TestOrchestrator(Base):
    def test_full_workflow_and_idempotency(self):
        d = syn.load_basic(self.c, ADMIN, PREPARER)
        bank = d["accounts"]["Operating bank"]
        ids = orch.submit_import_workflow(self.c, PREPARER, "csv", bank, syn.BANK_CSV, "2026-09")
        res = orch.run_pending(self.c)
        self.assertEqual(res, {"done": 8, "failed": 0, "retried": 0, "blocked": 0})
        self.assertEqual(ids, orch.submit_import_workflow(self.c, PREPARER, "csv", bank, syn.BANK_CSV, "2026-09"))  # same ids, nothing re-queued
        self.assertEqual(orch.run_pending(self.c)["done"], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM transactions")[0], 14)
        self.assertGreater(self.one("SELECT COUNT(*) FROM approvals WHERE type='classification_review'")[0], 0)
        rep = json.loads(self.one("SELECT result_json FROM tasks WHERE type='reconcile'")[0])
        self.assertEqual(rep["status"], "incomplete")
        s = orch.summary(self.c)
        self.assertTrue(s["audit_chain_ok"])
        self.assertFalse(s["cash"]["available_operating_cash"]["available"] is True and s["cash"]["complete"] is False)

    def test_failed_dependency_blocks_downstream_and_raises_material_flag(self):
        t1 = orch.submit(self.c, PREPARER, "import_csv", {"account_id": 999, "content": CSV})
        t2 = orch.submit(self.c, PREPARER, "categorise", {}, depends_on=[t1])
        res = orch.run_pending(self.c)
        self.assertEqual((res["failed"], res["blocked"]), (1, 1))
        self.assertTrue(self.one("SELECT 1 FROM flags WHERE entity_type='task' AND code='task_failed' AND severity='material'"))
        self.assertEqual(self.one("SELECT status FROM tasks WHERE id=?", t2)[0], "blocked")

    def test_transient_errors_retry_then_succeed_and_then_exhaust(self):
        calls = {"n": 0}
        def flaky(c, u, p):
            calls["n"] += 1
            if calls["n"] < 3:
                raise orch.TransientError("rate limited")
            return {"ok": True}
        orch.HANDLERS["flaky"] = flaky
        self.addCleanup(orch.HANDLERS.pop, "flaky")
        t = orch.submit(self.c, PREPARER, "flaky", {})
        self.assertEqual(orch.run_pending(self.c)["retried"], 1)
        self.assertEqual(orch.run_pending(self.c)["done"], 0)  # backoff: not due yet
        later = orch.run_pending(self.c, at="2999-01-01T00:00:00Z")  # attempt 2 fails again, attempt 3 succeeds within the sweep
        self.assertEqual((later["retried"], later["done"]), (1, 1))
        row = self.one("SELECT status, attempts FROM tasks WHERE id=?", t)
        self.assertEqual((row[0], row[1]), ("done", 3))
        orch.HANDLERS["always"] = lambda c, u, p: (_ for _ in ()).throw(orch.TransientError("down"))
        self.addCleanup(orch.HANDLERS.pop, "always")
        t2 = orch.submit(self.c, PREPARER, "always", {}, max_attempts=2)
        for _ in range(4):
            orch.run_pending(self.c, at="2999-01-01T00:00:00Z")
        self.assertEqual(self.one("SELECT status FROM tasks WHERE id=?", t2)[0], "failed")
        self.assertIn("retries exhausted", self.one("SELECT error FROM tasks WHERE id=?", t2)[0])

    def test_output_validation_rejects_float_money_bad_currency_bad_dates(self):
        for bad in ({"amount_minor": 12.5}, {"x": {"minor": 1.5, "currency": "SGD"}}, {"currency": "sgd1"}, {"due_date": "31/02/2026"}):
            with self.assertRaises(orch.ValidationFailure, msg=str(bad)):
                orch.validate_output(bad)
        orch.validate_output({"x": {"minor": None, "currency": "SGD"}, "a_minor": 5, "as_of": "2026-09-30"})

    def test_viewer_cannot_submit_tasks(self):
        with self.assertRaises(Forbidden):
            orch.submit(self.c, VIEWER, "categorise", {})


if __name__ == "__main__":
    unittest.main()
