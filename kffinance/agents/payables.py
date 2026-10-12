"""Agent 4 - Expenses, procurement and accounts payable.

Prepares and tracks. It can never pay, commit spend or change payment details; those are approvals for a human.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from ..core import ControlViolation, User, audit, request_approval, tx
from ..db import ensure_period, now, setting
from ..ingest import extract_document_fields
from ..money import format_minor, obj
from .bookkeeping import raise_flag


def submit_invoice(conn, user: User, direction: str, counterparty: str, number: str, issue_date: str, due_date: str | None,
                   amount_minor: int, tax_minor: int, currency: str, product: str | None = None, payment_terms: str | None = None) -> dict:
    """Creates an invoice in 'pending' approval status and an approval request. Flags probable duplicates."""
    user.require("enter_docs")
    if amount_minor <= 0 or tax_minor < 0:
        raise ControlViolation("invoice amount must be positive and tax non-negative")
    with tx(conn):
        period = ensure_period(conn, issue_date)
        try:
            cur = conn.execute("INSERT INTO invoices (direction,number,counterparty,issue_date,due_date,amount_minor,tax_minor,currency,product,payment_terms,accounting_period,created_at,updated_at) "
                               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (direction, number, counterparty, issue_date, due_date, amount_minor, tax_minor, currency, product, payment_terms, period, now(), now()))
        except Exception as ex:
            if "UNIQUE" in str(ex):
                raise ControlViolation(f"duplicate invoice: {direction} {counterparty} #{number} {currency} already exists") from None
            raise
        iid = cur.lastrowid
        near = conn.execute("""SELECT id FROM invoices WHERE direction=? AND lower(counterparty)=lower(?) AND amount_minor=? AND currency=? AND id<>?
                               AND abs(julianday(issue_date)-julianday(?))<=7""", (direction, counterparty, amount_minor, currency, iid, issue_date)).fetchone()
        if near:
            raise_flag(conn, "invoice", iid, "possible_duplicate_invoice", "material", f"Same counterparty, amount and week as invoice #{near[0]} with a different number")
        audit(conn, user.name, "invoice.submit", "invoice", iid, None, {"direction": direction, "counterparty": counterparty, "amount_minor": amount_minor})
    approval = request_approval(conn, user, "invoice", {"invoice_id": iid}, amount_minor + tax_minor, currency, idem=f"invoice:{iid}")
    return {"invoice_id": iid, "approval_id": approval, "duplicate_flag": bool(near)}


def submit_invoice_from_text(conn, user: User, text: str, direction="payable") -> dict:
    f = extract_document_fields(text)
    if not f["complete"]:
        missing = [k for k in ("vendor", "currency", "date", "amount") if f[k] is None]
        raise ControlViolation(f"could not extract {missing}; enter manually (nothing guessed)")
    number = f"EXT-{abs(hash(text)) % 10**8:08d}"
    return submit_invoice(conn, user, direction, f["vendor"], number, f["date"], None, f["amount"], f["tax"] or 0, f["currency"], payment_terms=f["payment_terms"])


def purchase_request(conn, user: User, description: str, amount_minor: int, currency: str, category: str, period: str, department="general") -> dict:
    """Spend commitments always need approval; outside-budget requests are marked as such."""
    b = conn.execute("SELECT * FROM budgets WHERE period=? AND department=? AND category=?", (period, department, category)).fetchone()
    spent = conn.execute("SELECT COALESCE(SUM(-amount_minor),0) FROM transactions WHERE category=? AND accounting_period=? AND classification_status='approved' AND currency=?",
                         (category, period, currency)).fetchone()[0]
    committed = conn.execute("SELECT COALESCE(SUM(amount_minor),0) FROM approvals WHERE type='expenditure' AND status='approved' AND json_extract(payload_json,'$.category')=? AND json_extract(payload_json,'$.period')=?",
                             (category, period)).fetchone()[0]
    within = bool(b) and b["currency"] == currency and spent + committed + amount_minor <= (b["spend_limit_minor"] or b["amount_minor"])
    check = {"budget_exists": bool(b), "within_approved_limit": within, "spent_minor": spent, "committed_minor": committed}
    aid = request_approval(conn, user, "expenditure", {"description": description, "category": category, "period": period, "department": department, "budget_check": check},
                           amount_minor, currency)
    return {"approval_id": aid, "budget_check": check, "outside_approved_limit": not within}


def authorise_payment(conn, user: User, invoice_id: int) -> dict:
    """Creates a payment authorisation request. The system does not move money; a human pays in the bank."""
    inv = conn.execute("SELECT * FROM invoices WHERE id=? AND direction='payable'", (invoice_id,)).fetchone()
    if not inv or inv["approval_status"] != "approved":
        raise ControlViolation("invoice must be approved before a payment can be authorised")
    if conn.execute("SELECT 1 FROM flags WHERE entity_type='invoice' AND entity_id=? AND status='open' AND severity='material'", (str(invoice_id),)).fetchone():
        raise ControlViolation("invoice has an open material flag")
    aid = request_approval(conn, user, "payment", {"invoice_id": invoice_id, "counterparty": inv["counterparty"], "note": "authorisation only; pay manually"},
                           inv["amount_minor"] + inv["tax_minor"], inv["currency"], idem=f"payment:{invoice_id}")
    return {"approval_id": aid, "note": "Authorisation only. No payment is executed by this system."}


def request_bank_detail_change(conn, user: User, vendor: str, new_details_ref: str) -> dict:
    aid = request_approval(conn, user, "vendor_change", {"vendor": vendor, "new_details_ref": new_details_ref})
    return {"approval_id": aid}


def upcoming_payables(conn, as_of: str, days=30, base="SGD") -> dict:
    end = (date.fromisoformat(as_of) + timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT * FROM invoices WHERE direction='payable' AND status='open' AND approval_status='approved' AND COALESCE(due_date,issue_date)<=? ORDER BY COALESCE(due_date,issue_date)", (end,)).fetchall()
    return {"as_of": as_of, "window_days": days, "items": [{"id": r["id"], "counterparty": r["counterparty"], "number": r["number"], "due_date": r["due_date"],
            "overdue": (r["due_date"] or r["issue_date"]) < as_of, "amount": obj(r["amount_minor"] + r["tax_minor"], r["currency"])} for r in rows],
            "total_base_currency": obj(sum(r["amount_minor"] + r["tax_minor"] for r in rows if r["currency"] == base), base),
            "other_currency_items": sum(1 for r in rows if r["currency"] != base)}


def recurring_vendors(conn) -> list:
    rows = conn.execute("""SELECT counterparty, currency, COUNT(DISTINCT substr(transaction_date,1,7)) months, AVG(-amount_minor) avg_amt, MIN(-amount_minor) lo, MAX(-amount_minor) hi
                           FROM transactions WHERE kind='expense' AND counterparty IS NOT NULL GROUP BY counterparty, currency HAVING months>=3""").fetchall()
    return [{"vendor": r["counterparty"], "months_seen": r["months"], "average": obj(int(r["avg_amt"]), r["currency"]),
             "stable": (r["hi"] - r["lo"]) <= 0.1 * r["avg_amt"]} for r in rows]


def savings_opportunities(conn, base="SGD") -> list:
    """Evidence-only observations; nothing here is invented."""
    out = []
    for v in recurring_vendors(conn):
        out.append({"type": "recurring_vendor_review", "detail": f"{v['vendor']}: recurring {v['average']['display']}/month over {v['months_seen']} months - confirm still needed"})
    top = conn.execute("SELECT category, SUM(-amount_minor) s FROM transactions WHERE kind='expense' AND currency=? AND category IS NOT NULL GROUP BY category ORDER BY s DESC LIMIT 3", (base,)).fetchall()
    for r in top:
        out.append({"type": "top_spend_category", "detail": f"Category {r['category']} totals {format_minor(r['s'], base)}"})
    return out
