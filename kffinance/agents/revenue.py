"""Agent 3 - Revenue, billing and pricing.

Cash arriving is not revenue and a webhook is not settlement. Revenue figures come from approved invoices;
cash receipts, refunds, fees and customer funds are reported separately with explicit definitions.
"""
from __future__ import annotations

import json
from datetime import date

from ..core import User
from ..money import obj

DEFINITIONS = {
    "gross_revenue": "Approved receivable invoices issued in the period, excluding tax, before refunds.",
    "refunds": "Settled refund transactions in the period (credits to customers).",
    "net_revenue": "gross_revenue - refunds. Processor fees are not netted here.",
    "processor_fees": "Settled fee transactions (e.g. Stripe fees) in the period.",
    "contribution_margin": "net_revenue - processor_fees - hosting/infrastructure (account 5100, approved).",
    "cash_receipts": "Settled income transactions in operating accounts. NOT revenue: shown for cash tracking only.",
    "customer_funds": "Receipts in customer-funds accounts. Held for others; excluded from revenue and from available cash.",
}


def revenue_report(conn, period_from: str, period_to: str, base="SGD") -> dict:
    rng = (period_from, period_to)
    inv = conn.execute("""SELECT product, counterparty, substr(issue_date,1,7) period, SUM(amount_minor) net FROM invoices
        WHERE direction='receivable' AND approval_status='approved' AND status IN ('open','paid') AND currency=? AND issue_date>=? AND issue_date<=?
        GROUP BY product, counterparty, period""", (base, *rng)).fetchall()
    gross = sum(r["net"] for r in inv)

    def q(sql, *a):
        return conn.execute(sql, a).fetchone()[0] or 0
    refunds = -q("SELECT SUM(amount_minor) FROM transactions WHERE kind='refund' AND status='settled' AND currency=? AND transaction_date>=? AND transaction_date<=?", base, *rng)
    fees = -q("SELECT SUM(amount_minor) FROM transactions WHERE kind='fee' AND status='settled' AND currency=? AND transaction_date>=? AND transaction_date<=?", base, *rng)
    hosting = -q("SELECT SUM(amount_minor) FROM transactions WHERE category='5100' AND classification_status='approved' AND status='settled' AND currency=? AND transaction_date>=? AND transaction_date<=?", base, *rng)
    cash_op = q("""SELECT SUM(t.amount_minor) FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE t.kind='income' AND t.status='settled'
                   AND a.funds_class='operating' AND t.currency=? AND t.transaction_date>=? AND t.transaction_date<=?""", base, *rng)
    cust = q("""SELECT SUM(t.amount_minor) FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE t.kind='income' AND t.status='settled'
                AND a.funds_class='customer_funds' AND t.currency=? AND t.transaction_date>=? AND t.transaction_date<=?""", base, *rng)
    unsettled = q("SELECT SUM(amount_minor) FROM transactions WHERE kind='income' AND status='pending' AND currency=?", base)
    net = gross - refunds
    today = date.today().isoformat()
    ar = conn.execute("SELECT * FROM invoices WHERE direction='receivable' AND status='open' AND approval_status='approved' AND currency=?", (base,)).fetchall()
    overdue = [r for r in ar if (r["due_date"] or "9999") < today]
    # events: dedupe by object id so payment_intent + charge events for one payment are not double counted
    ev = conn.execute("""SELECT object_id, MAX(CASE WHEN event_type LIKE '%failed' THEN 1 ELSE 0 END) failed,
                         MAX(CASE WHEN event_type LIKE '%succeeded' THEN 1 ELSE 0 END) ok, MIN(settlement_status) st, MAX(amount_minor) amt
                         FROM payment_events WHERE object_id IS NOT NULL GROUP BY object_id""").fetchall()
    by_product: dict = {}
    by_customer: dict = {}
    by_period: dict = {}
    for r in inv:
        by_product[r["product"] or "unspecified"] = by_product.get(r["product"] or "unspecified", 0) + r["net"]
        by_customer[r["counterparty"]] = by_customer.get(r["counterparty"], 0) + r["net"]
        by_period[r["period"]] = by_period.get(r["period"], 0) + r["net"]
    has_data = conn.execute("SELECT COUNT(*) FROM invoices WHERE direction='receivable' AND approval_status='approved'").fetchone()[0] > 0
    return {
        "period": {"from": period_from, "to": period_to}, "currency": base, "definitions": DEFINITIONS,
        "data_available": has_data,
        "gross_revenue": obj(gross if has_data else None, base), "refunds": obj(refunds, base), "net_revenue": obj(net if has_data else None, base),
        "processor_fees": obj(fees, base), "contribution_margin": obj(net - fees - hosting if has_data else None, base),
        "cash_receipts_not_revenue": obj(cash_op, base), "customer_funds_not_revenue": obj(cust, base), "unsettled_income": obj(unsettled, base),
        "by_product": {k: obj(v, base) for k, v in sorted(by_product.items())},
        "by_customer": {k: obj(v, base) for k, v in sorted(by_customer.items())},
        "by_period": {k: obj(v, base) for k, v in sorted(by_period.items())},
        "receivables_outstanding": obj(sum(r["amount_minor"] + r["tax_minor"] for r in ar), base),
        "overdue_invoices": [{"id": r["id"], "counterparty": r["counterparty"], "number": r["number"], "due_date": r["due_date"],
                              "amount": obj(r["amount_minor"] + r["tax_minor"], base)} for r in overdue],
        "payment_events": {"distinct_payments": len(ev), "failed": sum(1 for e in ev if e["failed"]), "succeeded_unsettled": sum(1 for e in ev if e["ok"] and e["st"] == "unsettled"),
                           "note": "Events are source signals. 'succeeded_unsettled' have no settled balance transaction yet and are not counted as cash or revenue."},
    }


def pricing_scenario(base_customers: int, price_minor: int, new_price_minor: int, churn_pct_change_per_10pct_price: float, currency="SGD",
                     monthly_churn_pct: float = 0.0) -> dict:
    """Explicit-assumption price change model. Elasticity is an assumption supplied by the caller, not an estimate from data."""
    chg = (new_price_minor - price_minor) / price_minor * 100
    extra_churn = max(0.0, chg / 10 * churn_pct_change_per_10pct_price)
    retained = base_customers * (1 - extra_churn / 100)
    cur, new = base_customers * price_minor, round(retained * new_price_minor)
    return {"currency": currency, "assumptions": {"base_customers": base_customers, "price_change_pct": round(chg, 2),
            "extra_churn_pct_from_change": round(extra_churn, 2), "elasticity_input": churn_pct_change_per_10pct_price},
            "current_monthly": obj(cur, currency), "projected_monthly": obj(new, currency), "delta_monthly": obj(new - cur, currency),
            "basis": "assumption", "uncertainty": "Elasticity is an input assumption. Treat the result as illustrative, not a forecast of outcomes."}
