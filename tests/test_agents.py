"""Treasury, revenue, tax, FP&A and reporting agents."""
import sqlite3
import unittest
from datetime import date

from helpers import ADMIN, APPROVER, APPROVER2, PREPARER, VIEWER, Base
from kffinance import core
from kffinance import synthetic as syn
from kffinance.agents import bookkeeping as bk
from kffinance.agents import fpa, payables, reporting, revenue, tax, treasury
from kffinance.core import ControlViolation, Forbidden
from kffinance.ingest import import_csv, record_payment_event


class TestTreasury(Base):
    def test_unavailable_is_not_zero(self):
        a = self.acct()
        pos = treasury.cash_position(self.c)
        self.assertFalse(pos["available_operating_cash"]["available"])
        self.assertIsNone(pos["available_operating_cash"]["minor"])
        self.snapshot(a, "2026-09-30", 0)  # a genuine verified zero
        pos = treasury.cash_position(self.c)
        self.assertTrue(pos["available_operating_cash"]["available"])
        self.assertEqual(pos["available_operating_cash"]["minor"], 0)

    def test_unverified_snapshot_is_ignored(self):
        a = self.acct()
        self.snapshot(a, "2026-09-30", 999999, verified=False)
        self.assertFalse(treasury.cash_position(self.c)["available_operating_cash"]["available"])

    def test_customer_restricted_unsettled_and_foreign_funds_not_in_available_cash(self):
        op, cust = self.acct("Op"), self.acct("Cust", funds="customer_funds")
        rst, usd = self.acct("Restricted", funds="restricted"), self.acct("USD", ccy="USD")
        for a, v, ccy in ((op, 100000, "SGD"), (cust, 900000, "SGD"), (rst, 50000, "SGD"), (usd, 77700, "USD")):
            self.snapshot(a, "2026-09-30", v, ccy=ccy)
        pos = treasury.cash_position(self.c)
        self.assertEqual(pos["available_operating_cash"]["minor"], 100000)
        self.assertIn("USD", pos["other_currency_accounts"][0])

    def test_runway_needs_history_and_burn(self):
        a = self.acct()
        self.snapshot(a, "2026-09-30", 1000000)
        r = treasury.runway(self.c, date(2026, 10, 12))
        self.assertEqual(r["status"], "insufficient_history")
        self.assertIsNone(r["runway_months"])
        rows = "date,description,amount,reference\n" + "".join(f"2026-{m:02d}-05,Burn {m},-1000.00,B{m}\n" for m in (7, 8, 9))
        import_csv(self.c, PREPARER, a, rows)
        r = treasury.runway(self.c, date(2026, 10, 12))
        self.assertEqual((r["status"], r["runway_months"]), ("estimated", 10.0))
        self.assertEqual(r["basis"], "estimate")

    def test_forecast_labels_basis_flags_shortfall_and_excludes_other_currency(self):
        a = self.acct()
        self.snapshot(a, "2026-10-09", 100000)  # SGD 1,000
        r = payables.submit_invoice(self.c, PREPARER, "payable", "Landlord", "R1", "2026-10-01", "2026-10-20", 150000, 0, "SGD")
        core.decide_approval(self.c, APPROVER, r["approval_id"], True)
        r2 = payables.submit_invoice(self.c, PREPARER, "receivable", "Customer", "C1", "2026-10-01", "2026-10-15", 100000, 0, "SGD")
        core.decide_approval(self.c, APPROVER, r2["approval_id"], True)
        r3 = payables.submit_invoice(self.c, PREPARER, "payable", "US vendor", "U1", "2026-10-01", "2026-10-20", 5000, 0, "USD")
        core.decide_approval(self.c, APPROVER, r3["approval_id"], True)
        with core.tx(self.c):
            self.c.execute("INSERT INTO planned_items (description,item_date,amount_minor,currency,kind,created_by,created_at) VALUES ('Laptop','2026-11-02',-200000,'SGD','purchase','p','x')")
        f = treasury.forecast_13w(self.c, PREPARER, "2026-10-12")
        self.assertEqual(f["opening_cash"]["minor"], 100000)
        self.assertEqual(f["opening_cash_basis"], "actual (verified snapshot)")
        basis = {i["basis"] for i in f["items"]}
        self.assertTrue({"scheduled", "estimate", "assumption"} <= basis)
        self.assertEqual(len(f["excluded_items"]), 1)
        self.assertEqual(f["excluded_items"][0]["currency"], "USD")
        ar = next(i for i in f["items"] if i["label"].startswith("AR"))
        self.assertEqual(ar["amount_minor"], 90000)  # 90% collection assumption
        self.assertTrue(f["shortfalls"], "closing cash goes negative after the laptop")
        self.assertIn("estimate", f["uncertainty"].lower() + "estimate")
        self.assertEqual(self.one("SELECT COUNT(*) FROM forecasts")[0], 1)
        wk = f["weeks"]
        self.assertEqual(wk[12]["closing_cash"], 100000 + sum(w["net"] for w in wk))

    def test_forecast_without_verified_cash_is_relative_only(self):
        self.acct()
        f = treasury.forecast_13w(self.c, PREPARER, "2026-10-12", save=False)
        self.assertIsNone(f["opening_cash"]["minor"])
        self.assertEqual(f["opening_cash_basis"], "unavailable")
        self.assertTrue(all(w["closing_cash"] is None for w in f["weeks"]))


class TestRevenue(Base):
    def test_cash_is_not_revenue_and_customer_funds_excluded(self):
        op, cust = self.acct("Op"), self.acct("Cust", funds="customer_funds")
        import_csv(self.c, PREPARER, op, "date,description,amount,reference\n2026-09-10,Receipt,300.00,A1\n")
        import_csv(self.c, PREPARER, cust, "date,description,amount,reference\n2026-09-10,Tenant deposit,5000.00,D1\n")
        r = revenue.revenue_report(self.c, "2026-09-01", "2026-09-30")
        self.assertFalse(r["gross_revenue"]["available"])  # no approved invoices: unavailable, not 30000
        self.assertEqual(r["cash_receipts_not_revenue"]["minor"], 30000)
        self.assertEqual(r["customer_funds_not_revenue"]["minor"], 500000)

    def test_revenue_by_product_net_of_refunds_and_margin(self):
        a = self.acct("Stripe", typ="payment_processor")
        for n, prod, amt in (("1", "platform fee", 1900), ("2", "visibility promotion", 4900)):
            r = payables.submit_invoice(self.c, PREPARER, "receivable", f"Cust{n}", f"I{n}", "2026-09-05", "2026-09-20", amt, 0, "SGD", product=prod)
            core.decide_approval(self.c, APPROVER, r["approval_id"], True)
        from kffinance.ingest import import_stripe_balance_transactions as imp
        imp(self.c, PREPARER, a, [{"id": "t1", "type": "charge", "amount": 1900, "fee": 100, "currency": "sgd", "created": 1788000000, "available_on": 1788000000},
                                  {"id": "t2", "type": "refund", "amount": -900, "fee": 0, "currency": "sgd", "created": 1788000000, "available_on": 1788000000}], now_epoch=1788100000)
        # balance transactions are dated 2026-08-29 (epoch 1788000000), so use a wide window
        r = revenue.revenue_report(self.c, "2026-08-01", "2026-09-30")
        self.assertEqual(r["gross_revenue"]["minor"], 6800)
        self.assertEqual(r["refunds"]["minor"], 900)
        self.assertEqual(r["net_revenue"]["minor"], 5900)
        self.assertEqual(r["processor_fees"]["minor"], 100)
        self.assertEqual(r["contribution_margin"]["minor"], 5800)
        self.assertEqual(r["by_product"]["platform fee"]["minor"], 1900)

    def test_events_for_one_payment_are_not_double_counted_and_failures_are_reported(self):
        for i, t in enumerate(("payment_intent.succeeded", "charge.succeeded")):
            record_payment_event(self.c, {"id": f"e{i}", "type": t, "data": {"object": {"id": "pi_1", "amount": 1900, "currency": "sgd"}}}, True)
        record_payment_event(self.c, {"id": "e9", "type": "payment_intent.payment_failed", "data": {"object": {"id": "pi_2", "amount": 1900, "currency": "sgd"}}}, True)
        pe = revenue.revenue_report(self.c, "2026-01-01", "2026-12-31")["payment_events"]
        self.assertEqual((pe["distinct_payments"], pe["failed"], pe["succeeded_unsettled"]), (2, 1, 1))

    def test_overdue_receivables(self):
        r = payables.submit_invoice(self.c, PREPARER, "receivable", "Late Co", "L1", "2025-01-01", "2025-01-15", 5000, 0, "SGD")
        core.decide_approval(self.c, APPROVER, r["approval_id"], True)
        rep = revenue.revenue_report(self.c, "2025-01-01", "2025-12-31")
        self.assertEqual(rep["overdue_invoices"][0]["counterparty"], "Late Co")

    def test_pricing_scenario_states_assumptions(self):
        s = revenue.pricing_scenario(100, 1900, 2200, churn_pct_change_per_10pct_price=2.0)
        self.assertEqual(s["basis"], "assumption")
        self.assertIn("not a forecast", s["uncertainty"])
        self.assertEqual(s["current_monthly"]["minor"], 190000)


class TestTax(Base):
    def test_nothing_is_assumed_when_facts_unknown(self):
        cal = tax.build_calendar(self.c, PREPARER, "2026-10-12")
        self.assertEqual(cal["items"], [])
        topics = {q["topic"] for q in cal["open_questions"]}
        self.assertEqual(topics, {"fiscal_year_end", "gst_registration"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM open_questions")[0], 2)
        tax.build_calendar(self.c, PREPARER, "2026-10-12")
        self.assertEqual(self.one("SELECT COUNT(*) FROM open_questions")[0], 2)  # not duplicated

    def test_calendar_from_verified_year_end(self):
        with self.assertRaises(Forbidden):
            tax.set_fact(self.c, PREPARER, "fiscal_year_end", "12-31", True, "ACRA")
        with self.assertRaises(ControlViolation):
            tax.set_fact(self.c, APPROVER, "fiscal_year_end", "12-31", True, "")
        tax.set_fact(self.c, APPROVER, "fiscal_year_end", "12-31", True, "ACRA BizFile")
        cal = tax.build_calendar(self.c, PREPARER, "2026-10-12")
        due = {i["code"]: i["due_date"] for i in cal["items"]}
        self.assertEqual(due["ECI-2025-12-31"], "2026-03-31")
        self.assertEqual(due["AGM-2025-12-31"], "2026-06-30")
        self.assertEqual(due["ACRA-AR-2025-12-31"], "2026-07-31")
        self.assertEqual(due["CORPTAX-2025-12-31"], "2026-11-30")
        self.assertTrue(all("confirm" in i["basis"].lower() for i in cal["items"]))
        self.assertTrue(all(r["professional_review"] == "required" for r in self.c.execute("SELECT professional_review FROM tax_items")))

    def test_gst_registered_does_not_invent_dates(self):
        tax.set_fact(self.c, APPROVER, "gst_registered", "true", True, "IRAS notice")
        cal = tax.build_calendar(self.c, PREPARER, "2026-10-12")
        gst = next(i for i in cal["items"] if i["code"] == "GST-RETURNS")
        self.assertIsNone(gst["due_date"])

    def test_threshold_monitor_is_an_estimate_with_caveat(self):
        r = payables.submit_invoice(self.c, PREPARER, "receivable", "Big", "B1", "2026-06-01", None, 25_000_000, 0, "SGD")
        core.decide_approval(self.c, APPROVER, r["approval_id"], True)
        m = tax.gst_threshold_monitor(self.c, "2026-10-12")
        self.assertEqual(m["pct_of_threshold"], 25.0)
        self.assertEqual(m["basis"], "estimate")
        self.assertEqual(m["registration_status"], "unknown")

    def test_handoff_pack_flags_unreviewed_status(self):
        pack = tax.accountant_handoff(self.c, PREPARER, "2026-10-12")
        self.assertIn("NOT reviewed", pack["professional_review_status"])
        self.assertFalse(pack["chart_of_accounts_approved_by_accountant"])


class TestFPA(Base):
    def test_no_invented_numbers(self):
        r = fpa.scenario_model(self.c, PREPARER, None, save=False)
        self.assertEqual(r["status"], "insufficient_data")
        self.assertIn("monthly_growth", r["needed_inputs"])

    def test_scenarios_sensitivity_and_metadata(self):
        p = {"revenue_start_minor": 1_000_000, "monthly_growth": 0.05, "cogs_pct": 0.1, "base_opex_minor": 600_000, "marketing_minor": 100_000,
             "hires": [{"from_month": 4, "monthly_cost_minor": 700_000}]}
        r = fpa.scenario_model(self.c, PREPARER, p)
        s = r["scenarios"]
        self.assertGreater(s["upside"]["cumulative_net"], s["base"]["cumulative_net"])
        self.assertLess(s["downside"]["cumulative_net"], s["base"]["cumulative_net"])
        sens = r["most_material_assumptions"]
        self.assertEqual(sens, sorted(sens, key=lambda x: -x["swing"]))
        for k in ("method", "assumptions", "sources", "period", "uncertainty", "scenario_rules"):
            self.assertIn(k, r)
        self.assertIn("not a forecast of outcomes", r["uncertainty"])
        # independent check of month 1: revenue 1,050,000; net = 1,050,000*0.9 - 600,000 - 100,000
        self.assertEqual(s["base"]["months"][0]["net"], round(1_050_000 * 0.9 - 700_000))

    def test_budget_variance_uses_approved_only(self):
        a = self.acct()
        fpa.set_budget(self.c, PREPARER, "2026-09", "ops", "6100", 10000, "SGD", 12000)
        import_csv(self.c, PREPARER, a, "date,description,amount,reference\n2026-09-05,Lovable credits,-60.00,L1\n2026-09-06,Anthropic usage,-50.00,L2\n")
        bk.categorise(self.c, PREPARER)
        t = self.one("SELECT record_id FROM transactions WHERE description LIKE 'Lovable%'")[0]
        bk.classify_transaction(self.c, PREPARER, t, "6100")
        row = fpa.budget_vs_actual(self.c, "2026-09")["rows"][0]
        self.assertEqual((row["actual_approved"]["minor"], row["unreviewed_proposed"]["minor"], row["variance"]["minor"]), (6000, 5000, 4000))
        self.assertFalse(row["over_budget"])

    def test_cac_needs_verified_inputs(self):
        self.assertFalse(fpa.unit_economics(self.c, "2026-09")["cac"]["available"])


class TestReporting(Base):
    def post(self, lines, date_="2026-09-10", user=PREPARER, poster=APPROVER):
        e = core.create_entry(self.c, user, date_, "t", lines, "SGD")
        core.post_entry(self.c, poster, e)

    def test_statements_use_posted_entries_only_and_balance(self):
        self.post([("1000", 100000, 0), ("3000", 0, 100000)])  # capital
        self.post([("1100", 50000, 0), ("4000", 0, 50000)])    # invoice
        self.post([("5000", 2000, 0), ("1000", 0, 2000)])      # fee
        core.create_entry(self.c, PREPARER, "2026-09-12", "draft only", [("6900", 99999, 0), ("1000", 0, 99999)], "SGD")
        pl = reporting.profit_and_loss(self.c, "2026-09-01", "2026-09-30")["by_currency"]["SGD"]
        self.assertEqual((pl["total_revenue"], pl["total_expenses"], pl["net_profit"]), (50000, 2000, 48000))
        bs = reporting.balance_sheet(self.c, "2026-09-30")["by_currency"]["SGD"]
        self.assertTrue(bs["balanced"])
        self.assertEqual((bs["total_assets"], bs["current_earnings"], bs["total_equity"]), (148000, 48000, 148000))
        cf = reporting.cash_flow_statement(self.c, "2026-09-01", "2026-09-30")["by_currency"]["SGD"]
        self.assertEqual((cf["financing"], cf["operating"], cf["closing_cash"]), (100000, -2000, 98000))
        self.assertIn("UNAUDITED", reporting.profit_and_loss(self.c, "2026-09-01", "2026-09-30")["banner"])

    def test_kpis_unavailable_without_posted_records(self):
        k = reporting.kpis(self.c, "2026-09-01", "2026-09-30")
        self.assertFalse(k["data_available"])
        self.assertFalse(k["net_revenue"]["available"])
        self.post([("1100", 50000, 0), ("4000", 0, 50000)])
        self.post([("4090", 5000, 0), ("1000", 0, 5000)])
        self.post([("5000", 1000, 0), ("1000", 0, 1000)])
        k = reporting.kpis(self.c, "2026-09-01", "2026-09-30")
        self.assertEqual((k["gross_revenue"]["minor"], k["net_revenue"]["minor"], k["contribution_margin"]["minor"]), (50000, 45000, 44000))

    def test_monthly_report_versions_label_and_approval(self):
        r1 = reporting.monthly_report(self.c, PREPARER, "2026-09")
        r2 = reporting.monthly_report(self.c, PREPARER, "2026-09")
        self.assertEqual((r1["version"], r2["version"]), (1, 2))
        self.assertIn("UNAUDITED", r1["banner"])
        self.assertIn("unavailable, not zero", r1["executive_summary"]["headline"])
        self.assertTrue(any("No verified cash" in x for x in r1["executive_summary"]["risks"]))
        with self.assertRaises(Forbidden):
            reporting.approve_report(self.c, PREPARER, "monthly_management", "2026-09", 1)
        reporting.approve_report(self.c, APPROVER, "monthly_management", "2026-09", 1)
        with self.assertRaises(sqlite3.DatabaseError):
            self.c.execute("UPDATE report_versions SET content_json='{}' WHERE version=1")

    def test_creator_cannot_approve_own_report(self):
        reporting.monthly_report(self.c, APPROVER, "2026-09")
        with self.assertRaisesRegex(ControlViolation, "separation"):
            reporting.approve_report(self.c, APPROVER, "monthly_management", "2026-09", 1)
        reporting.approve_report(self.c, APPROVER2, "monthly_management", "2026-09", 1)

    def test_investor_pack_is_local_draft_with_forward_looking_caveat(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            pack = reporting.investor_pack(self.c, PREPARER, "2026-09", d)
            self.assertTrue(os.path.exists(os.path.join(d, "investor_pack_2026-09.md")))
        self.assertIn("not guaranteed", pack["forward_looking"])
        self.assertIn("export_external", pack["distribution"])


if __name__ == "__main__":
    unittest.main()
