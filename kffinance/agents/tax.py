"""Agent 5 - Tax and accounting compliance (Singapore).

Organises and flags. It never submits anything and never assumes GST status or tax treatment.
Every obligation is 'potentially applicable' and needs professional confirmation.
"""
from __future__ import annotations

import json
from datetime import date

from ..core import ControlViolation, User, audit, tx
from ..db import now
from ..money import obj

GST_THRESHOLD_MINOR = 100_000_000  # S$1,000,000 taxable turnover (retrospective test uses calendar year; prospective uses next 12 months)
VERIFY = "Rule as understood by this system - confirm current IRAS/ACRA requirements with your accountant."


def set_fact(conn, user: User, key: str, value: str, verified: bool, source: str):
    user.require("approve")  # facts drive compliance logic: approver or admin only
    if key not in {r[0] for r in conn.execute("SELECT key FROM company_facts")}:
        raise ControlViolation("unknown company fact")
    if verified and not source:
        raise ControlViolation("verified facts need a source reference (e.g. IRAS/ACRA document)")
    if key == "fiscal_year_end":
        month, day = value.split("-")
        date(2000, int(month), int(day))  # validates MM-DD
    with tx(conn):
        before = dict(conn.execute("SELECT * FROM company_facts WHERE key=?", (key,)).fetchone())
        conn.execute("UPDATE company_facts SET value=?, verified=?, source=?, updated_by=?, updated_at=? WHERE key=?",
                     (value, int(verified), source, user.name, now(), key))
        audit(conn, user.name, "tax.set_fact", "company_fact", key, before, {"value": value, "verified": verified, "source": source})


def _fact(conn, key):
    r = conn.execute("SELECT * FROM company_facts WHERE key=?", (key,)).fetchone()
    return r["value"], bool(r["verified"])


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    from calendar import monthrange
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def build_calendar(conn, user: User, as_of: str) -> dict:
    user.require("plan")
    today = date.fromisoformat(as_of)
    fye, fye_ok = _fact(conn, "fiscal_year_end")
    gst, gst_ok = _fact(conn, "gst_registered")
    items, questions = [], []
    with tx(conn):
        def item(code, title, due, basis, notes=None):
            conn.execute("INSERT OR IGNORE INTO tax_items (code,title,due_date,basis,notes) VALUES (?,?,?,?,?)", (code, title, due, basis, notes))
            items.append({"code": code, "title": title, "due_date": due, "basis": basis, "notes": notes})

        def question(topic, q):
            if not conn.execute("SELECT 1 FROM open_questions WHERE topic=? AND status='open'", (topic,)).fetchone():
                conn.execute("INSERT INTO open_questions (topic,question,created_at) VALUES (?,?,?)", (topic, q, now()))
            questions.append({"topic": topic, "question": q})

        if not fye:
            question("fiscal_year_end", "What is KeyFables' financial year-end? Filing deadlines cannot be computed without it.")
        else:
            m, d = (int(x) for x in fye.split("-"))
            for yr in (today.year - 1, today.year, today.year + 1):
                fy_end = date(yr, m, d) if not (m == 2 and d == 29) else date(yr, 2, 28)
                if abs((fy_end - today).days) > 400:
                    continue
                tag = fy_end.isoformat()
                item(f"ECI-{tag}", f"Estimated Chargeable Income (ECI) filing for FYE {tag}", _add_months(fy_end, 3).isoformat(),
                     f"Within 3 months of financial year-end; may be waived if annual revenue <= S$5m and ECI is nil. {VERIFY}")
                item(f"AGM-{tag}", f"AGM / members' resolution for FYE {tag}", _add_months(fy_end, 6).isoformat(), f"Private companies: within 6 months of FYE. {VERIFY}")
                item(f"ACRA-AR-{tag}", f"ACRA annual return for FYE {tag}", _add_months(fy_end, 7).isoformat(), f"Within 7 months of FYE for non-listed companies. {VERIFY}")
                item(f"CORPTAX-{tag}", f"Corporate income tax return (Form C-S/C) for FYE {tag}", date(fy_end.year + 1, 11, 30).isoformat(),
                     f"Due 30 November of the year after the FYE year. {VERIFY}")
        if gst is None:
            question("gst_registration", "Is KeyFables registered for GST? (status unknown - no GST treatment is assumed)")
        elif gst.lower() in ("true", "yes", "1"):
            item("GST-RETURNS", "GST returns (filing frequency and dates assigned by IRAS)", None,
                 f"Requires the IRAS-assigned accounting period; dates not computed without it. {VERIFY}")
        elif not gst_ok:
            question("gst_registration", "GST status is recorded as not registered but is unverified: provide evidence.")
        if not fye_ok and fye:
            question("fiscal_year_end_verification", "Year-end is recorded but unverified; confirm against ACRA records.")
        audit(conn, user.name, "tax.build_calendar", None, None, None, {"items": len(items), "questions": len(questions)})
    return {"as_of": as_of, "items": items, "open_questions": questions, "disclaimer": "Potentially applicable obligations only; not tax advice."}


def gst_threshold_monitor(conn, as_of: str, base="SGD") -> dict:
    start = date.fromisoformat(as_of).replace(year=date.fromisoformat(as_of).year - 1).isoformat()
    inv = conn.execute("SELECT COALESCE(SUM(amount_minor),0) FROM invoices WHERE direction='receivable' AND approval_status='approved' AND status IN ('open','paid') AND currency=? AND issue_date>? AND issue_date<=?",
                       (base, start, as_of)).fetchone()[0]
    gst, ok = _fact(conn, "gst_registered")
    return {"trailing_12m_invoiced": obj(inv, base), "threshold": obj(GST_THRESHOLD_MINOR, base), "pct_of_threshold": round(inv / GST_THRESHOLD_MINOR * 100, 2),
            "registration_status": gst if gst else "unknown", "verified": ok, "basis": "estimate",
            "caveat": "Taxable turnover cannot be determined from invoice totals (exempt/zero-rated/out-of-scope supplies differ). An accountant must assess the retrospective and prospective tests."}


def missing_documentation(conn) -> list:
    rows = conn.execute("""SELECT t.record_id, t.transaction_date, t.counterparty, t.amount_minor, t.currency, t.category FROM transactions t
        WHERE t.kind='expense' AND t.status='settled' AND NOT EXISTS (SELECT 1 FROM matches m WHERE m.transaction_id=t.record_id AND m.status='confirmed')""").fetchall()
    return [{"record_id": r["record_id"], "date": r["transaction_date"], "counterparty": r["counterparty"], "amount": obj(-r["amount_minor"], r["currency"]),
             "category": r["category"], "issue": "no supporting document matched"} for r in rows]


def deductible_register(conn) -> dict:
    """Candidate deductible expenses with evidence status. Deductibility itself is for the accountant."""
    ev = conn.execute("""SELECT t.record_id, t.transaction_date d, t.counterparty cp, -t.amount_minor amt, t.currency ccy, t.category cat,
        EXISTS (SELECT 1 FROM matches m WHERE m.transaction_id=t.record_id AND m.status='confirmed') has_doc
        FROM transactions t WHERE t.kind IN ('expense','fee') AND t.status='settled' AND t.classification_status='approved' ORDER BY t.transaction_date""").fetchall()
    return {"note": "Candidates only; deductibility is not determined here.", "with_evidence": [dict(r) for r in ev if r["has_doc"]], "without_evidence": [dict(r) for r in ev if not r["has_doc"]]}


def submit_tax_return(*a, **k):
    raise ControlViolation("Tax return submission is not supported by design. Prepare the handoff pack and have a qualified professional file it.")


def accountant_handoff(conn, user: User, as_of: str, out_path: str | None = None) -> dict:
    user.require("read")
    facts = [dict(r) for r in conn.execute("SELECT key,value,verified,source FROM company_facts")]
    pack = {"title": "Accountant handoff package (DRAFT - unaudited management records)", "as_of": as_of, "generated_by": user.name,
            "company_facts": facts,
            "calendar": [dict(r) for r in conn.execute("SELECT code,title,due_date,basis,status,professional_review FROM tax_items ORDER BY due_date")],
            "open_questions": [dict(r) for r in conn.execute("SELECT topic,question FROM open_questions WHERE status='open'")],
            "periods": [dict(r) for r in conn.execute("SELECT id,status FROM periods ORDER BY id")],
            "open_flags": [dict(r) for r in conn.execute("SELECT entity_type,entity_id,code,severity,detail FROM flags WHERE status='open'")],
            "expenses_without_documents": len(missing_documentation(conn)),
            "unposted_journal_drafts": conn.execute("SELECT COUNT(*) FROM journal_entries WHERE status='draft'").fetchone()[0],
            "chart_of_accounts_approved_by_accountant": conn.execute("SELECT value FROM settings WHERE key='coa_approved_by_accountant'").fetchone()[0] == "1",
            "professional_review_status": "NOT reviewed. No conclusion in this pack is a tax determination."}
    with tx(conn):
        audit(conn, user.name, "tax.handoff_generated", None, None, None, {"as_of": as_of, "open_flags": len(pack["open_flags"])})
    if out_path:
        with open(out_path, "w") as f:
            json.dump(pack, f, indent=2)
    return pack
