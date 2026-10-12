"""Agent 7 - CFO, governance and investor reporting.

Statements are built only from POSTED journal entries and are always labelled as unaudited management drafts.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta

from ..core import ControlViolation, User, audit, trial_balance, tx
from ..db import month_bounds, now, setting
from ..money import format_minor, obj
from . import fpa, revenue, treasury

BANNER = "UNAUDITED MANAGEMENT REPORT - DRAFT. Not audited, reviewed or independently certified."
CASH_CODES = ("1000", "1010", "1050")


def _by_ccy(rows):
    out: dict = {}
    for r in rows:
        out.setdefault(r["currency"], []).append(r)
    return out


def profit_and_loss(conn, date_from: str, date_to: str) -> dict:
    res = {}
    for ccy, rows in _by_ccy(trial_balance(conn, up_to=date_to, period_from=date_from)).items():
        rev = [(r["code"], r["name"], r["cr"] - r["dr"]) for r in rows if r["type"] == "revenue"]
        exp = [(r["code"], r["name"], r["dr"] - r["cr"]) for r in rows if r["type"] == "expense"]
        tr, te = sum(x[2] for x in rev), sum(x[2] for x in exp)
        res[ccy] = {"revenue": [{"code": c, "name": n, "amount": a} for c, n, a in rev], "expenses": [{"code": c, "name": n, "amount": a} for c, n, a in exp],
                    "total_revenue": tr, "total_expenses": te, "net_profit": tr - te}
    return {"statement": "Profit and loss (draft)", "from": date_from, "to": date_to, "by_currency": res, "banner": BANNER,
            "basis": "posted journal entries only; accrual per invoices; coa pending accountant approval" if setting(conn, "coa_approved_by_accountant") != "1" else "posted journal entries only"}


def balance_sheet(conn, as_of: str) -> dict:
    res = {}
    for ccy, rows in _by_ccy(trial_balance(conn, up_to=as_of)).items():
        a = sum(r["dr"] - r["cr"] for r in rows if r["type"] == "asset")
        l = sum(r["cr"] - r["dr"] for r in rows if r["type"] == "liability")
        e = sum(r["cr"] - r["dr"] for r in rows if r["type"] == "equity")
        earnings = sum(r["cr"] - r["dr"] for r in rows if r["type"] == "revenue") - sum(r["dr"] - r["cr"] for r in rows if r["type"] == "expense")
        res[ccy] = {"assets": [{"code": r["code"], "name": r["name"], "amount": r["dr"] - r["cr"]} for r in rows if r["type"] == "asset"],
                    "liabilities": [{"code": r["code"], "name": r["name"], "amount": r["cr"] - r["dr"]} for r in rows if r["type"] == "liability"],
                    "equity": [{"code": r["code"], "name": r["name"], "amount": r["cr"] - r["dr"]} for r in rows if r["type"] == "equity"],
                    "current_earnings": earnings, "total_assets": a, "total_liabilities": l, "total_equity": e + earnings,
                    "balanced": a == l + e + earnings}
    return {"statement": "Balance sheet (draft)", "as_of": as_of, "by_currency": res, "banner": BANNER}


def cash_flow_statement(conn, date_from: str, date_to: str) -> dict:
    """Direct method from journal entries that touch cash accounts, classified by counter-account type."""
    res: dict = {}
    for e in conn.execute("SELECT id,currency FROM journal_entries WHERE status IN ('posted','reversed') AND entry_date>=? AND entry_date<=?", (date_from, date_to)):
        lines = conn.execute("SELECT l.account_code,l.debit_minor,l.credit_minor,c.type FROM journal_lines l JOIN coa c ON c.code=l.account_code WHERE entry_id=?", (e["id"],)).fetchall()
        cash = sum(l["debit_minor"] - l["credit_minor"] for l in lines if l["account_code"] in CASH_CODES)
        if not cash:
            continue
        others = [l for l in lines if l["account_code"] not in CASH_CODES]
        section = "financing" if any(l["type"] == "equity" for l in others) else "operating"
        r = res.setdefault(e["currency"], {"operating": 0, "investing": 0, "financing": 0})
        r[section] += cash
    for ccy, r in res.items():
        opening = sum(x["dr"] - x["cr"] for x in trial_balance(conn, up_to=(date.fromisoformat(date_from) - timedelta(days=1)).isoformat())
                      if x["currency"] == ccy and x["code"] in CASH_CODES)
        r["net_change"] = r["operating"] + r["investing"] + r["financing"]
        r["opening_cash"] = opening
        r["closing_cash"] = opening + r["net_change"]
    return {"statement": "Cash flow statement (draft, direct method)", "from": date_from, "to": date_to, "by_currency": res, "banner": BANNER}


def kpis(conn, date_from: str, date_to: str, base="SGD") -> dict:
    tb = {r["code"]: r for r in trial_balance(conn, up_to=date_to, period_from=date_from) if r["currency"] == base}
    def cr(c): return tb[c]["cr"] - tb[c]["dr"] if c in tb else 0
    def dr(c): return tb[c]["dr"] - tb[c]["cr"] if c in tb else 0
    any_posted = bool(tb)
    gross = cr("4000") + cr("4100") + cr("4900")
    refunds = dr("4090")
    net = gross - refunds
    cm = net - dr("5000") - dr("5100")
    run = treasury.runway(conn, date.fromisoformat(date_to), base)
    defs = {r["key"]: r["definition"] for r in conn.execute("SELECT key,definition FROM kpi_definitions")}
    return {"period": {"from": date_from, "to": date_to}, "definitions": defs, "data_available": any_posted,
            "gross_revenue": obj(gross if any_posted else None, base), "net_revenue": obj(net if any_posted else None, base),
            "contribution_margin": obj(cm if any_posted else None, base), "net_burn": run.get("net_burn"), "runway_months": run.get("runway_months"),
            "runway_status": run["status"]}


def _summary(conn, period, k, cash, run) -> dict:
    risks, actions = [], []
    n_flags = conn.execute("SELECT COUNT(*) FROM flags WHERE status='open'").fetchone()[0]
    n_mat = conn.execute("SELECT COUNT(*) FROM flags WHERE status='open' AND severity='material'").fetchone()[0]
    n_app = conn.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
    n_draft = conn.execute("SELECT COUNT(*) FROM journal_entries WHERE status='draft'").fetchone()[0]
    unrec = conn.execute("SELECT COUNT(*) FROM transactions WHERE accounting_period=? AND reconciliation_status IN ('unreconciled','exception')", (period,)).fetchone()[0]
    if not cash["available_operating_cash"]["available"]:
        risks.append("No verified cash balance: cash position and runway cannot be stated.")
        actions.append("Record verified bank/Stripe balance snapshots with evidence.")
    if run["status"] == "estimated" and run["runway_months"] is not None and run["runway_months"] < 6:
        risks.append(f"Estimated runway {run['runway_months']} months (< 6).")
        actions.append("Review burn and funding plan.")
    if n_mat:
        risks.append(f"{n_mat} material open flag(s).")
        actions.append("Resolve material flags before closing the period.")
    if unrec:
        risks.append(f"{unrec} transaction(s) in {period} are unreconciled.")
    if n_draft:
        actions.append(f"{n_draft} journal draft(s) await review and posting.")
    if n_app:
        actions.append(f"{n_app} approval(s) pending.")
    gst = conn.execute("SELECT value,verified FROM company_facts WHERE key='gst_registered'").fetchone()
    if not gst["verified"]:
        risks.append("GST registration status is unverified; no GST treatment is assumed.")
    headline = ("No posted accounting records for this period yet; figures below are unavailable, not zero." if not k["data_available"]
                else f"Net revenue {k['net_revenue']['display']}, contribution margin {k['contribution_margin']['display']} (posted journals, unaudited).")
    return {"headline": headline, "risks": risks, "recommended_actions": actions, "open_flags": n_flags, "pending_approvals": n_app}


def monthly_report(conn, user: User, period: str, as_of: str | None = None) -> dict:
    user.require("plan")
    d_from, d_to = month_bounds(period)
    as_of = as_of or d_to
    base = setting(conn, "base_currency")
    k = kpis(conn, d_from, d_to, base)
    cash = treasury.cash_position(conn, base)
    run = treasury.runway(conn, date.fromisoformat(d_to), base)
    content = {"banner": BANNER, "period": period, "generated_at": now(), "executive_summary": _summary(conn, period, k, cash, run), "kpis": k, "cash_position": cash, "runway": run,
               "profit_and_loss": profit_and_loss(conn, d_from, d_to), "balance_sheet": balance_sheet(conn, d_to), "cash_flow": cash_flow_statement(conn, d_from, d_to),
               "revenue": revenue.revenue_report(conn, d_from, d_to, base), "budget_vs_actual": fpa.budget_vs_actual(conn, period),
               "period_status": (conn.execute("SELECT status FROM periods WHERE id=?", (period,)).fetchone() or ["not_started"])[0]}
    return save_version(conn, user, "monthly_management", period, content)


def save_version(conn, user: User, rtype: str, period: str, content: dict) -> dict:
    body = json.dumps(content, sort_keys=True, default=str)
    with tx(conn):
        v = (conn.execute("SELECT COALESCE(MAX(version),0) FROM report_versions WHERE report_type=? AND period=?", (rtype, period)).fetchone()[0]) + 1
        conn.execute("INSERT INTO report_versions (report_type,period,version,content_json,content_sha256,created_by,created_at) VALUES (?,?,?,?,?,?,?)",
                     (rtype, period, v, body, hashlib.sha256(body.encode()).hexdigest(), user.name, now()))
        audit(conn, user.name, "report.version", "report", f"{rtype}/{period}", None, {"version": v})
    return {"report_type": rtype, "period": period, "version": v, "status": "draft", **content}


def approve_report(conn, user: User, rtype: str, period: str, version: int):
    user.require("approve_report")
    with tx(conn):
        r = conn.execute("SELECT * FROM report_versions WHERE report_type=? AND period=? AND version=?", (rtype, period, version)).fetchone()
        if not r or r["status"] != "draft":
            raise ControlViolation("no such draft version")
        if setting(conn, "enforce_separation_of_duties") == "1" and r["created_by"] == user.name:
            raise ControlViolation("separation of duties: creator cannot approve their own report")
        conn.execute("UPDATE report_versions SET status='approved', approved_by=? WHERE id=?", (user.name, r["id"]))
        audit(conn, user.name, "report.approve", "report", f"{rtype}/{period}", {"version": version, "status": "draft"}, {"status": "approved"})


def investor_pack(conn, user: User, period: str, out_dir: str | None = None) -> dict:
    """Local export only. Sending it outside the company requires an approved 'export_external' approval."""
    user.require("plan")
    rep = monthly_report(conn, user, period)
    pack = {"title": f"KeyFables investor update - {period}", "banner": BANNER,
            "summary": rep["executive_summary"], "kpis": rep["kpis"], "cash": rep["cash_position"]["available_operating_cash"], "runway": rep["runway"],
            "statements": {"profit_and_loss": rep["profit_and_loss"], "balance_sheet": rep["balance_sheet"], "cash_flow": rep["cash_flow"]},
            "forward_looking": "Any projection is an estimate based on stated assumptions and is not guaranteed.",
            "distribution": "Internal draft. External sharing requires an approved export_external approval."}
    if out_dir:
        import os
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, f"investor_pack_{period}.json"), "w") as f:
            json.dump(pack, f, indent=2, default=str)
        lines = [f"# {pack['title']}", "", f"**{BANNER}**", "", f"**Headline:** {rep['executive_summary']['headline']}", "", "## Risks"]
        lines += [f"- {r}" for r in rep["executive_summary"]["risks"]] or ["- none recorded"]
        lines += ["", "## Recommended actions"] + ([f"- {a}" for a in rep["executive_summary"]["recommended_actions"]] or ["- none"])
        lines += ["", "## Cash", f"Available operating cash: {pack['cash']['display']}", "", pack["forward_looking"]]
        with open(os.path.join(out_dir, f"investor_pack_{period}.md"), "w") as f:
            f.write("\n".join(lines))
    return pack
