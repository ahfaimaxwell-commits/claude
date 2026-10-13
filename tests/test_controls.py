"""Accounting calculations and controls: money, journals, audit chain, approvals, RBAC."""
import sqlite3
import unittest

from helpers import ADMIN, APPROVER, APPROVER2, PREPARER, VIEWER, Base
from kffinance import core
from kffinance.agents import payables, tax
from kffinance.core import ControlViolation, Forbidden
from kffinance.money import FxRate, MoneyError, add, convert, format_minor, parse_amount


class TestMoney(unittest.TestCase):
    def test_parse_and_format(self):
        self.assertEqual(parse_amount("1,234.50", "SGD"), 123450)
        self.assertEqual(parse_amount("(12.00)", "SGD"), -1200)
        self.assertEqual(parse_amount("1000", "JPY"), 1000)
        self.assertEqual(format_minor(-123450, "SGD"), "-SGD 1,234.50")

    def test_rejects_excess_precision_instead_of_rounding(self):
        with self.assertRaises(MoneyError):
            parse_amount("1.005", "SGD")
        with self.assertRaises(MoneyError):
            parse_amount("abc", "SGD")
        with self.assertRaises(MoneyError):
            parse_amount("NaN", "SGD")

    def test_no_implicit_currency_mixing(self):
        with self.assertRaises(MoneyError):
            add(100, "SGD", 100, "USD")
        with self.assertRaises(MoneyError):
            convert(100, "USD", "SGD", None)
        self.assertEqual(convert(10000, "USD", "SGD", FxRate("USD", "SGD", "1.35", "MAS", "2026-09-30")), 13500)
        with self.assertRaises(MoneyError):
            convert(100, "USD", "SGD", FxRate("EUR", "SGD", "1.5", "x", "2026-09-30"))


class TestJournal(Base):
    def draft(self, lines=None, date="2026-09-10", user=PREPARER, ccy="SGD", **kw):
        lines = lines or [("1000", 1000, 0), ("4000", 0, 1000)]
        return core.create_entry(self.c, user, date, "test", lines, ccy, **kw)

    def test_balanced_entry_posts(self):
        e = self.draft()
        core.post_entry(self.c, APPROVER, e)
        self.assertEqual(self.one("SELECT status FROM journal_entries WHERE id=?", e)[0], "posted")

    def test_unbalanced_entry_cannot_post(self):
        e = self.draft([("1000", 1000, 0), ("4000", 0, 900)])
        with self.assertRaisesRegex(ControlViolation, "not balanced"):
            core.post_entry(self.c, APPROVER, e)
        self.assertEqual(self.one("SELECT status FROM journal_entries WHERE id=?", e)[0], "draft")

    def test_mixed_currency_lines_rejected(self):
        e = self.draft()
        self.c.execute("UPDATE journal_lines SET currency='USD' WHERE entry_id=? AND account_code='4000'", (e,))
        with self.assertRaisesRegex(ControlViolation, "currency"):
            core.post_entry(self.c, APPROVER, e)

    def test_posted_entries_are_immutable(self):
        e = self.draft()
        core.post_entry(self.c, APPROVER, e)
        for sql in ("UPDATE journal_entries SET memo='x' WHERE id=?", "DELETE FROM journal_entries WHERE id=?",
                    "UPDATE journal_lines SET debit_minor=5 WHERE entry_id=?", "DELETE FROM journal_lines WHERE entry_id=?",
                    "INSERT INTO journal_lines (entry_id,account_code,debit_minor,credit_minor,currency) SELECT ?,'1000',1,0,'SGD'"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.c.execute(sql, (e,))

    def test_separation_of_duties_on_posting(self):
        e = self.draft(user=APPROVER)
        with self.assertRaisesRegex(ControlViolation, "separation of duties"):
            core.post_entry(self.c, APPROVER, e)
        core.post_entry(self.c, APPROVER2, e)

    def test_preparer_cannot_post(self):
        e = self.draft()
        with self.assertRaises(Forbidden):
            core.post_entry(self.c, PREPARER, e)

    def test_closed_period_blocks_posting(self):
        e = self.draft(date="2026-08-10")
        a = core.request_approval(self.c, PREPARER, "period_close", {"period": "2026-08"})
        core.decide_approval(self.c, APPROVER, a, True)
        with self.assertRaisesRegex(ControlViolation, "not open"):
            core.post_entry(self.c, APPROVER, e)
        with self.assertRaises(sqlite3.DatabaseError):
            self.c.execute("UPDATE periods SET status='open' WHERE id='2026-08'")

    def test_reversal_is_the_only_correction(self):
        e = self.draft()
        core.post_entry(self.c, APPROVER, e)
        r = core.reverse_entry(self.c, PREPARER, e, "2026-09-20", "wrong account")
        core.finalize_reversal(self.c, APPROVER, r)
        self.assertEqual(self.one("SELECT status FROM journal_entries WHERE id=?", e)[0], "reversed")
        tb = {x["code"]: (x["dr"], x["cr"]) for x in core.trial_balance(self.c)}
        self.assertEqual(tb["1000"], (1000, 1000))  # nets to zero, history preserved
        with self.assertRaises(ControlViolation):
            core.reverse_entry(self.c, PREPARER, e, "2026-09-21", "again")

    def test_material_adjustment_needs_approval(self):
        e = self.draft([("6900", 600000, 0), ("1000", 0, 600000)], kind="adjustment")
        with self.assertRaisesRegex(ControlViolation, "material"):
            core.post_entry(self.c, APPROVER, e)
        a = core.request_approval(self.c, PREPARER, "material_adjustment", {"entry": e})
        core.decide_approval(self.c, APPROVER2, a, True)
        core.post_entry(self.c, APPROVER, e, a)


class TestAuditChain(Base):
    def test_chain_verifies_and_is_append_only(self):
        core.create_entry(self.c, PREPARER, "2026-09-10", "m", [("1000", 5, 0), ("4000", 0, 5)], "SGD")
        self.assertEqual(core.verify_audit_chain(self.c), (True, None))
        with self.assertRaises(sqlite3.DatabaseError):
            self.c.execute("UPDATE audit_log SET actor='mallory' WHERE id=1")
        with self.assertRaises(sqlite3.DatabaseError):
            self.c.execute("DELETE FROM audit_log WHERE id=1")

    def test_tampering_by_a_privileged_db_user_is_detected(self):
        core.create_entry(self.c, PREPARER, "2026-09-10", "m", [("1000", 5, 0), ("4000", 0, 5)], "SGD")
        self.c.execute("DROP TRIGGER audit_no_update")  # attacker with raw DB access
        self.c.execute("UPDATE audit_log SET actor='mallory' WHERE id=2")
        ok, bad = core.verify_audit_chain(self.c)
        self.assertFalse(ok)
        self.assertEqual(bad, 2)


class TestApprovalsAndRBAC(Base):
    def test_roles(self):
        with self.assertRaises(Forbidden):
            core.request_approval(self.c, VIEWER, "expenditure", {})
        a = core.request_approval(self.c, PREPARER, "expenditure", {"x": 1}, 100, "SGD")
        with self.assertRaises(Forbidden):
            core.decide_approval(self.c, PREPARER, a, True)

    def test_requester_cannot_decide_own_request(self):
        a = core.request_approval(self.c, APPROVER, "expenditure", {}, 100, "SGD")
        with self.assertRaisesRegex(ControlViolation, "separation of duties"):
            core.decide_approval(self.c, APPROVER, a, True)
        core.decide_approval(self.c, APPROVER2, a, True)
        with self.assertRaises(ControlViolation):  # cannot decide twice
            core.decide_approval(self.c, APPROVER2, a, False)

    def test_request_is_idempotent(self):
        a = core.request_approval(self.c, PREPARER, "payment", {}, idem="pay:1")
        self.assertEqual(a, core.request_approval(self.c, PREPARER, "payment", {}, idem="pay:1"))

    def test_permission_changes_need_admin_approval(self):
        a = core.request_approval(self.c, ADMIN, "permission_change", {"action": "create_user", "name": "new", "role": "viewer"})
        with self.assertRaises(ControlViolation):  # approver but not admin
            core.decide_approval(self.c, APPROVER, a, True)
        admin2 = core.User(9, "admin2", "finance_admin")
        core.create_user(self.c, "admin2", "finance_admin")
        res = core.decide_approval(self.c, admin2, a, True)
        tok = res["result"]["token_shown_once"]
        self.assertEqual(core.authenticate(self.c, tok).role, "viewer")
        self.assertIsNone(core.authenticate(self.c, "wrong"))

    def test_payment_authorisation_never_executes(self):
        r = payables.submit_invoice(self.c, PREPARER, "payable", "Vendor X", "V1", "2026-09-01", "2026-09-30", 50000, 0, "SGD")
        with self.assertRaisesRegex(ControlViolation, "approved"):
            payables.authorise_payment(self.c, PREPARER, r["invoice_id"])
        core.decide_approval(self.c, APPROVER, r["approval_id"], True)
        p = payables.authorise_payment(self.c, PREPARER, r["invoice_id"])
        res = core.decide_approval(self.c, APPROVER2, p["approval_id"], True)
        self.assertEqual(self.one("SELECT status FROM approvals WHERE id=?", p["approval_id"])[0], "approved")  # never 'executed'
        self.assertIn("never moves money", res["note"])

    def test_duplicate_payable_invoices(self):
        payables.submit_invoice(self.c, PREPARER, "payable", "Vendor X", "V1", "2026-09-01", None, 50000, 0, "SGD")
        with self.assertRaisesRegex(ControlViolation, "duplicate invoice"):
            payables.submit_invoice(self.c, PREPARER, "payable", "Vendor X", "V1", "2026-09-02", None, 50000, 0, "SGD")
        r = payables.submit_invoice(self.c, PREPARER, "payable", "Vendor X", "V1-b", "2026-09-03", None, 50000, 0, "SGD")
        self.assertTrue(r["duplicate_flag"])

    def test_bank_detail_change_requires_approval(self):
        r = payables.request_bank_detail_change(self.c, PREPARER, "Vendor X", "ref-new")
        self.assertIsNone(self.one("SELECT bank_details_ref FROM vendors WHERE name='Vendor X'"))
        core.decide_approval(self.c, APPROVER, r["approval_id"], True)
        self.assertEqual(self.one("SELECT bank_details_ref FROM vendors WHERE name='Vendor X'")[0], "ref-new")

    def test_tax_submission_is_never_automated(self):
        with self.assertRaisesRegex(ControlViolation, "not supported"):
            tax.submit_tax_return()
        a = core.request_approval(self.c, PREPARER, "tax_submission", {"professional_review_completed": False})
        with self.assertRaisesRegex(ControlViolation, "professional review"):
            core.decide_approval(self.c, APPROVER, a, True)


if __name__ == "__main__":
    unittest.main()


class TestAmendPaths(Base):
    def test_setting_change_needs_a_second_admin_and_is_validated(self):
        with self.assertRaisesRegex(ControlViolation, "cannot be changed"):
            core.validate_setting("base_currency", "USD")
        with self.assertRaises(ControlViolation):
            core.validate_setting("ar_collection_rate", "1.5")
        with self.assertRaises(ControlViolation):
            core.validate_setting("approval_threshold_minor", "-5")
        a = core.request_approval(self.c, ADMIN, "settings_change", {"key": "approval_threshold_minor", "value": "250000"})
        with self.assertRaises(ControlViolation):  # same person
            core.decide_approval(self.c, ADMIN, a, True)
        with self.assertRaises(ControlViolation):  # approver but not admin
            core.decide_approval(self.c, APPROVER, a, True)
        self.assertEqual(self.one("SELECT value FROM settings WHERE key='approval_threshold_minor'")[0], "100000")  # unchanged so far
        core.create_user(self.c, "admin2", "finance_admin")
        res = core.decide_approval(self.c, core.User(9, "admin2", "finance_admin"), a, True)
        self.assertEqual(res["result"], {"key": "approval_threshold_minor", "old": "100000", "new": "250000"})
        self.assertEqual(self.one("SELECT value FROM settings WHERE key='approval_threshold_minor'")[0], "250000")
        self.assertTrue(self.one("SELECT 1 FROM audit_log WHERE action='settings.change'"))
        self.assertEqual(core.verify_audit_chain(self.c), (True, None))

    def test_unvalidated_setting_cannot_be_smuggled_through_an_approval(self):
        a = core.request_approval(self.c, ADMIN, "settings_change", {"key": "base_currency", "value": "USD"})
        core.create_user(self.c, "admin2", "finance_admin")
        with self.assertRaises(ControlViolation):
            core.decide_approval(self.c, core.User(9, "admin2", "finance_admin"), a, True)
        self.assertEqual(self.one("SELECT value FROM settings WHERE key='base_currency'")[0], "SGD")
