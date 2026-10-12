"""Agent 2 - Treasury and cash flow.

Cash is reported only from verified snapshots. Everything else is labelled with its basis:
actual | scheduled | estimate | assumption.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from ..core import ControlViolation, User, audit, tx
from ..db import now, setting
from ..money import format_minor, obj


def cash_position(conn, base="SGD") -> dict:
    accounts = []
    for a in conn.execute("SELECT * FROM accounts WHERE active=1 ORDER BY id"):
        snap = conn.execute("SELECT * FROM balance_snapshots WHERE account_id=? AND verified=1 ORDER BY as_of DESC LIMIT 1", (a["id"],)).fetchone()
        pending = conn.execute("SELECT COALESCE(SUM(amount_minor),0) FROM transactions WHERE account_id=? AND status='pending'", (a["id"],)).fetchone()[0]
        after = (conn.execute("SELECT COALESCE(SUM(amount_minor),0) FROM transactions WHERE account_id=? AND status='settled' AND transaction_date>?",
                              (a["id"], snap["as_of"])).fetchone()[0] if snap else None)
        accounts.append({"account_id": a["id"], "name": a["name"], "funds_class": a["funds_class"], "currency": a["currency"],
                         "balance": obj(snap["amount_minor"] if snap else None, a["currency"]),
                         "as_of": snap["as_of"] if snap else None, "evidence_ref": snap["evidence_ref"] if snap else None,
                         "source": snap["source"] if snap else None,
                         "unsettled": obj(pending, a["currency"]),
                         "movements_since_snapshot_unverified": obj(after, a["currency"]) if snap else obj(None, a["currency"])})
    op = [x for x in accounts if x["funds_class"] == "operating" and x["currency"] == base]
    complete = bool(op) and all(x["balance"]["available"] for x in op)
    total = sum(x["balance"]["minor"] for x in op if x["balance"]["available"]) if op and any(x["balance"]["available"] for x in op) else None
    return {"base_currency": base,
            "available_operating_cash": obj(total if complete else None, base),
            "complete": complete,
            "note": ("All operating accounts have verified balances." if complete else
                     "Unavailable: at least one operating account has no verified balance snapshot." if op else "Unavailable: no operating accounts configured."),
            "accounts": accounts,
            "excluded_from_available_cash": "restricted funds, customer funds held for others, and unsettled payments are shown but never added to available cash",
            "other_currency_accounts": [x["name"] for x in accounts if x["currency"] != base]}


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _monthly_net(conn, base, as_of, months=3):
    first = date(as_of.year, as_of.month, 1)
    out = []
    for i in range(1, months + 1):
        m_end = first
        for _ in range(i - 1):
            m_end = (m_end - timedelta(days=1)).replace(day=1)
        end = m_end
        start = (end - timedelta(days=1)).replace(day=1)
        r = conn.execute("""SELECT COALESCE(SUM(t.amount_minor),0) s, COUNT(*) n FROM transactions t JOIN accounts a ON a.id=t.account_id
            WHERE t.status='settled' AND a.funds_class='operating' AND t.currency=? AND t.kind IN ('income','expense','fee','refund')
            AND t.transaction_date>=? AND t.transaction_date<?""", (base, start.isoformat(), end.isoformat())).fetchone()
        out.append({"month": start.isoformat()[:7], "net_minor": r["s"], "transactions": r["n"]})
    return out


def runway(conn, as_of: date, base="SGD") -> dict:
    cash = cash_position(conn, base)
    months = _monthly_net(conn, base, as_of)
    with_data = [m for m in months if m["transactions"] > 0]
    res = {"basis": "estimate", "method": "verified operating cash / average monthly net burn over the last 3 full months of settled operating transactions (transfers and customer funds excluded)",
           "months_used": months, "cash": cash["available_operating_cash"]}
    if len(with_data) < 3:
        res.update(status="insufficient_history", runway_months=None, net_burn=None,
                   note=f"Only {len(with_data)} of 3 months have data; no runway is stated.")
        return res
    avg = sum(m["net_minor"] for m in months) / 3
    if avg >= 0:
        res.update(status="not_burning", runway_months=None, net_burn=obj(0, base), note="Average net flow is non-negative over the period.")
    elif not cash["available_operating_cash"]["available"]:
        res.update(status="cash_unavailable", runway_months=None, net_burn=obj(int(-avg), base), note="Burn is known but no verified cash balance exists.")
    else:
        res.update(status="estimated", net_burn=obj(int(-avg), base),
                   runway_months=round(cash["available_operating_cash"]["minor"] / -avg, 1),
                   note="Estimate; assumes recent burn continues and ignores funding or revenue changes.")
    return res


def forecast_13w(conn, user: User, as_of: str, scenario: str = "base", revenue_factor: float = 1.0, cost_factor: float = 1.0, save=True) -> dict:
    user.require("plan")
    base = setting(conn, "base_currency")
    d0 = date.fromisoformat(as_of)
    start = _monday(d0)
    end = start + timedelta(weeks=13)
    collect = float(setting(conn, "ar_collection_rate"))
    delay = int(setting(conn, "ar_collection_delay_days"))
    items, excluded = [], []

    def add(dt, minor, label, basis, ccy):
        if ccy != base:
            excluded.append({"label": label, "currency": ccy, "amount_minor": minor, "reason": "no explicit FX method configured"})
            return
        items.append({"date": max(dt, start).isoformat(), "amount_minor": int(minor), "label": label, "basis": basis})

    for inv in conn.execute("SELECT * FROM invoices WHERE status='open' AND approval_status='approved'"):
        due = date.fromisoformat(inv["due_date"] or inv["issue_date"])
        total = inv["amount_minor"] + inv["tax_minor"]
        if inv["direction"] == "receivable":
            add(due + timedelta(days=delay), round(total * collect * revenue_factor), f"AR {inv['counterparty']} #{inv['number']} (collection rate {collect:.0%}, +{delay}d)", "estimate", inv["currency"])
        else:
            add(due, -total, f"AP {inv['counterparty']} #{inv['number']}", "scheduled", inv["currency"])
    ap_vendors = {r[0].lower() for r in conn.execute("SELECT counterparty FROM invoices WHERE direction='payable' AND status='open'")}
    # recurring expense run-rate from actuals (skip vendors already covered by an open AP invoice)
    rec = conn.execute("""SELECT counterparty, currency, COUNT(DISTINCT substr(transaction_date,1,7)) months, AVG(-amount_minor) avg_amt, MAX(transaction_date) last_d
        FROM transactions WHERE kind='expense' AND status='settled' AND counterparty IS NOT NULL AND transaction_date>=? GROUP BY counterparty, currency HAVING months>=3""",
        ((d0 - timedelta(days=130)).isoformat(),)).fetchall()
    recurring = []
    for r in rec:
        if r["counterparty"].lower() in ap_vendors:
            continue
        nxt = date.fromisoformat(r["last_d"]) + timedelta(days=30)
        while nxt < end:
            if nxt >= start:
                add(nxt, -round(r["avg_amt"] * cost_factor), f"Recurring {r['counterparty']} (avg of {r['months']} months)", "estimate", r["currency"])
            nxt += timedelta(days=30)
        recurring.append(r["counterparty"])
    for p in conn.execute("SELECT * FROM planned_items WHERE item_date>=? AND item_date<?", (start.isoformat(), end.isoformat())):
        f = revenue_factor if p["kind"] == "revenue" else (cost_factor if p["amount_minor"] < 0 else 1.0)
        add(date.fromisoformat(p["item_date"]), round(p["amount_minor"] * f), f"Planned {p['kind']}: {p['description']}", "assumption", p["currency"])
    for t in conn.execute("SELECT t.* FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE t.status='pending' AND a.funds_class='operating'"):
        add(start, t["amount_minor"], f"Unsettled {t['kind']}: {t['description'][:40]}", "pending", t["currency"])

    cash = cash_position(conn, base)
    opening = cash["available_operating_cash"]["minor"] if cash["available_operating_cash"]["available"] else None
    weeks, running = [], opening
    for w in range(13):
        ws, we = start + timedelta(weeks=w), start + timedelta(weeks=w + 1)
        wi = [i for i in items if ws.isoformat() <= i["date"] < we.isoformat()]
        inflow = sum(i["amount_minor"] for i in wi if i["amount_minor"] > 0)
        outflow = sum(i["amount_minor"] for i in wi if i["amount_minor"] < 0)
        running = None if running is None else running + inflow + outflow
        weeks.append({"week": w + 1, "week_start": ws.isoformat(), "inflow": inflow, "outflow": outflow, "net": inflow + outflow, "closing_cash": running,
                      "by_basis": {b: sum(i["amount_minor"] for i in wi if i["basis"] == b) for b in sorted({i["basis"] for i in wi})}})
    cum = 0
    shortfalls = []
    for w in weeks:
        cum += w["net"]
        if (w["closing_cash"] is not None and w["closing_cash"] < 0) or (w["closing_cash"] is None and cum < 0):
            shortfalls.append({"week": w["week"], "week_start": w["week_start"], "closing_cash": w["closing_cash"], "cumulative_net": cum,
                               "note": "projected negative cash" if w["closing_cash"] is not None else "cumulative net outflow (opening cash unavailable, relative only)"})
    result = {"scenario": scenario, "as_of": as_of, "currency": base, "horizon_weeks": 13,
              "opening_cash": obj(opening, base), "opening_cash_basis": "actual (verified snapshot)" if opening is not None else "unavailable",
              "weeks": weeks, "items": items, "shortfalls": shortfalls, "excluded_items": excluded, "recurring_vendors_detected": recurring,
              "assumptions": {"ar_collection_rate": collect, "ar_collection_delay_days": delay, "revenue_factor": revenue_factor, "cost_factor": cost_factor,
                              "recurring_expenses": "projected at 30-day cadence from last 4 months' average", "overdue_items": "placed in week 1"},
              "basis_legend": {"actual": "verified opening cash", "scheduled": "approved AP invoices on due date", "pending": "unsettled processor items",
                               "estimate": "derived from receivables/recurring patterns", "assumption": "manually entered planned items"},
              "uncertainty": "Forecast, not a prediction. Receivable timing, collection rate and recurring patterns are estimates."}
    if save:
        with tx(conn):
            conn.execute("INSERT INTO forecasts (name,kind,as_of,method,assumptions_json,sources_json,result_json,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"13-week {scenario}", "cash_13w", as_of, "direct method: opening verified cash + dated inflows/outflows by basis",
                          json.dumps(result["assumptions"]), json.dumps(["invoices", "transactions", "planned_items", "balance_snapshots"]), json.dumps(result), user.name, now()))
            audit(conn, user.name, "treasury.forecast_13w", "forecast", None, None, {"scenario": scenario, "as_of": as_of})
    return result


def scenarios_13w(conn, user: User, as_of: str, save=True) -> dict:
    return {"base": forecast_13w(conn, user, as_of, "base", 1.0, 1.0, save),
            "downside": forecast_13w(conn, user, as_of, "downside", 0.7, 1.1, save),
            "upside": forecast_13w(conn, user, as_of, "upside", 1.15, 0.95, save)}
