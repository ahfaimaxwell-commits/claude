"""Agent 6 - Financial planning and analysis.

Every output carries its sources, assumptions, period, method and uncertainty. Nothing is presented as guaranteed.
"""
from __future__ import annotations

import json

from ..core import ControlViolation, User, audit, tx
from ..db import now
from ..money import obj


def set_budget(conn, user: User, period: str, department: str, category: str, amount_minor: int, currency: str, spend_limit_minor: int | None = None):
    user.require("plan")
    with tx(conn):
        conn.execute("INSERT INTO budgets (period,department,category,amount_minor,currency,spend_limit_minor,created_by,created_at) VALUES (?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(period,department,category) DO UPDATE SET amount_minor=excluded.amount_minor, spend_limit_minor=excluded.spend_limit_minor",
                     (period, department, category, amount_minor, currency, spend_limit_minor, user.name, now()))
        audit(conn, user.name, "budget.set", "budget", f"{period}/{department}/{category}", None, {"amount_minor": amount_minor, "limit": spend_limit_minor})


def budget_vs_actual(conn, period: str) -> dict:
    rows = []
    for b in conn.execute("SELECT * FROM budgets WHERE period=? ORDER BY department, category", (period,)):
        a = conn.execute("""SELECT COALESCE(SUM(CASE WHEN classification_status='approved' THEN -amount_minor END),0) ok,
                                   COALESCE(SUM(CASE WHEN classification_status<>'approved' THEN -amount_minor END),0) unreviewed
                            FROM transactions WHERE category=? AND accounting_period=? AND currency=? AND status='settled'""", (b["category"], period, b["currency"])).fetchone()
        var = b["amount_minor"] - a["ok"]
        lim = b["spend_limit_minor"]
        rows.append({"department": b["department"], "category": b["category"], "budget": obj(b["amount_minor"], b["currency"]), "actual_approved": obj(a["ok"], b["currency"]),
                     "unreviewed_proposed": obj(a["unreviewed"], b["currency"]), "variance": obj(var, b["currency"]),
                     "variance_pct": round(var / b["amount_minor"] * 100, 1) if b["amount_minor"] else None,
                     "over_budget": a["ok"] > b["amount_minor"], "over_spend_limit": bool(lim is not None and a["ok"] > lim)})
    return {"period": period, "rows": rows, "note": "Actuals use approved classifications only; proposed-but-unreviewed spend is shown separately.",
            "data_available": bool(rows)}


def _run(p: dict) -> dict:
    rev0, g, cogs, base, mkt = p["revenue_start_minor"], p["monthly_growth"], p["cogs_pct"], p["base_opex_minor"], p["marketing_minor"]
    months, cum, cum_rev = [], 0, 0
    for m in range(1, p["months"] + 1):
        rev = rev0 * (1 + g) ** m
        hire = sum(h["monthly_cost_minor"] for h in p["hires"] if h["from_month"] <= m)
        net = rev * (1 - cogs) - base - mkt - hire
        cum += net
        cum_rev += rev
        months.append({"month": m, "revenue": round(rev), "net": round(net), "cumulative_net": round(cum)})
    return {"months": months, "cumulative_net": round(cum), "total_revenue": round(cum_rev)}


def scenario_model(conn, user: User, params: dict | None = None, save=True) -> dict:
    """12-month base/upside/downside. Starting values default from verified actuals when available; else inputs are required."""
    user.require("plan")
    p = dict(params or {})
    last_rev = conn.execute("""SELECT SUM(amount_minor) FROM invoices WHERE direction='receivable' AND approval_status='approved' AND status IN ('open','paid')
                               AND issue_date>=date('now','start of month','-1 month') AND issue_date<date('now','start of month')""").fetchone()[0]
    needed = []
    p.setdefault("revenue_start_minor", last_rev)
    for k, d in (("monthly_growth", None), ("cogs_pct", None), ("base_opex_minor", None)):
        p.setdefault(k, d)
        if p[k] is None:
            needed.append(k)
    if p["revenue_start_minor"] is None:
        needed.append("revenue_start_minor")
    if needed:
        return {"status": "insufficient_data", "needed_inputs": needed, "note": "No figures are invented. Provide these inputs (with their source) to run the model."}
    p.setdefault("marketing_minor", 0)
    p.setdefault("hires", [])
    p.setdefault("months", 12)
    p.setdefault("currency", "SGD")
    scen = {"base": p,
            "upside": {**p, "monthly_growth": p["monthly_growth"] * 1.5 + 0.01, "cogs_pct": max(0, p["cogs_pct"] - 0.02)},
            "downside": {**p, "monthly_growth": p["monthly_growth"] * 0.5 - 0.01, "cogs_pct": p["cogs_pct"] + 0.03}}
    results = {k: _run(v) for k, v in scen.items()}
    sens = []
    drivers = {"monthly_growth": lambda q, f: {**q, "monthly_growth": q["monthly_growth"] * f}, "revenue_start_minor": lambda q, f: {**q, "revenue_start_minor": q["revenue_start_minor"] * f},
               "cogs_pct": lambda q, f: {**q, "cogs_pct": q["cogs_pct"] * f}, "base_opex_minor": lambda q, f: {**q, "base_opex_minor": q["base_opex_minor"] * f},
               "marketing_minor": lambda q, f: {**q, "marketing_minor": q["marketing_minor"] * f},
               "hire_costs": lambda q, f: {**q, "hires": [{**h, "monthly_cost_minor": h["monthly_cost_minor"] * f} for h in q["hires"]]}}
    base_cum = results["base"]["cumulative_net"]
    for name, fn in drivers.items():
        lo, hi = _run(fn(p, 0.9))["cumulative_net"], _run(fn(p, 1.1))["cumulative_net"]
        sens.append({"assumption": name, "delta_if_minus_10pct": lo - base_cum, "delta_if_plus_10pct": hi - base_cum, "swing": abs(hi - lo)})
    sens.sort(key=lambda s: -s["swing"])
    out = {"status": "ok", "period": f"next {p['months']} months", "currency": p["currency"], "method": "compound monthly revenue growth; COGS as % of revenue; fixed opex; hires from stated month",
           "assumptions": {k: p[k] for k in ("revenue_start_minor", "monthly_growth", "cogs_pct", "base_opex_minor", "marketing_minor", "hires", "months")},
           "scenario_rules": {"upside": "growth x1.5 + 1pp, COGS -2pp", "downside": "growth x0.5 - 1pp, COGS +3pp"},
           "sources": {"revenue_start_minor": "verified invoices last full month" if params is None or "revenue_start_minor" not in params else "user input", "others": "user input"},
           "scenarios": results, "most_material_assumptions": sens,
           "uncertainty": "Projection, not a forecast of outcomes or a valuation. Real results depend on factors not modelled; widen ranges accordingly."}
    if save:
        with tx(conn):
            conn.execute("INSERT INTO forecasts (name,kind,as_of,method,assumptions_json,sources_json,result_json,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         ("12-month scenarios", "fpa_12m", now()[:10], out["method"], json.dumps(out["assumptions"]), json.dumps(out["sources"]), json.dumps(out), user.name, now()))
            audit(conn, user.name, "fpa.scenarios", "forecast", None, None, {"months": p["months"]})
    return out


def unit_economics(conn, period: str, base="SGD") -> dict:
    def s(sql, *a):
        return conn.execute(sql, a).fetchone()[0]
    rev = s("SELECT COALESCE(SUM(amount_minor),0) FROM invoices WHERE direction='receivable' AND approval_status='approved' AND status IN ('open','paid') AND currency=? AND substr(issue_date,1,7)=?", base, period)
    cogs = -s("SELECT COALESCE(SUM(amount_minor),0) FROM transactions WHERE (kind='fee' OR category='5100') AND status='settled' AND currency=? AND accounting_period=?", base, period)
    mkt = -s("SELECT COALESCE(SUM(amount_minor),0) FROM transactions WHERE category='6200' AND classification_status='approved' AND status='settled' AND currency=? AND accounting_period=?", base, period)
    newc = conn.execute("SELECT value,verified FROM metrics_inputs WHERE key='new_customers' AND period=?", (period,)).fetchone()
    out = {"period": period, "gross_margin_pct": round((rev - cogs) / rev * 100, 1) if rev else None,
           "gross_margin_note": None if rev else "insufficient data: no approved receivable invoices in period"}
    if newc and newc["verified"] and newc["value"] > 0 and mkt > 0:
        out["cac"] = obj(round(mkt / newc["value"]), base)
        out["cac_basis"] = f"marketing spend (6200, approved) / {int(newc['value'])} verified new customers"
    else:
        out["cac"] = obj(None, base)
        out["cac_note"] = "insufficient data: needs verified new_customers and approved marketing spend for the period"
    return out
