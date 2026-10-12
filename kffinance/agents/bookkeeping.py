"""Agent 1 - Bookkeeping and reconciliation.

Proposes only. Classification approval, match confirmation and journal posting follow policy and RBAC.
"""
from __future__ import annotations

import json
import re
from datetime import date

from ..core import ControlViolation, User, audit, tx
from ..db import month_bounds, now, setting
from ..money import format_minor


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _sim(a, b):
    ta, tb = set(_norm(a).split()), set(_norm(b).split())
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def _contain(name, text):
    """Share of the counterparty/vendor name's words found in the (longer) bank text."""
    ta, tb = set(_norm(name).split()), set(_norm(text).split())
    return len(ta & tb) / len(ta) if ta else 0.0


def raise_flag(conn, entity_type, entity_id, code, severity, detail):
    conn.execute("INSERT OR IGNORE INTO flags (entity_type,entity_id,code,severity,detail,created_at) VALUES (?,?,?,?,?,?)",
                 (entity_type, str(entity_id), code, severity, detail, now()))


# ------------------------------------------------------------- categorisation


def categorise(conn, user: User) -> dict:
    """Rule-based proposals. Uncertain or unmatched rows go to the review queue; nothing self-approves."""
    user.require("classify")
    rules = conn.execute("SELECT * FROM category_rules WHERE active=1 ORDER BY confidence DESC").fetchall()
    auto = setting(conn, "auto_approve_classification") == "1"
    proposed = queued = 0
    with tx(conn):
        for t in conn.execute("SELECT * FROM transactions WHERE classification_status='uncategorised'").fetchall():
            text = _norm(f"{t['counterparty'] or ''} {t['description'] or ''}")
            hit = next((r for r in rules if r["pattern"] in text), None)
            if hit and hit["confidence"] >= 0.8:
                status = "approved" if (auto and hit["confidence"] >= 0.95 and abs(t["amount_minor"]) < int(setting(conn, "approval_threshold_minor"))) else "proposed"
                conn.execute("UPDATE transactions SET category=?, classification_status=?, confidence=?, updated_at=? WHERE record_id=?",
                             (hit["coa_code"], status, hit["confidence"], now(), t["record_id"]))
                proposed += 1
            else:
                raise_flag(conn, "transaction", t["record_id"], "needs_classification", "warning",
                           "No rule with sufficient confidence" if not hit else f"Low-confidence rule ({hit['confidence']})")
                queued += 1
        audit(conn, user.name, "bookkeeping.categorise", "transactions", None, None, {"proposed": proposed, "queued_for_review": queued})
    return {"proposed": proposed, "queued_for_review": queued}


def classify_transaction(conn, user: User, record_id: int, category: str, note: str = ""):
    """Human confirmation of a classification. Amounts above the threshold need an approver."""
    user.require("classify")
    with tx(conn):
        t = conn.execute("SELECT * FROM transactions WHERE record_id=?", (record_id,)).fetchone()
        if not t:
            raise ControlViolation("transaction not found")
        if t["classification_status"] == "approved":
            raise ControlViolation("already approved; use a reversal/correction")
        if not conn.execute("SELECT 1 FROM coa WHERE code=? AND active=1", (category,)).fetchone():
            raise ControlViolation("category is not in the chart of accounts")
        if abs(t["amount_minor"]) >= int(setting(conn, "approval_threshold_minor")) and not user.can("approve"):
            raise ControlViolation("amount above approval threshold: an approver must classify this transaction")
        conn.execute("UPDATE transactions SET category=?, classification_status='approved', approval_status='approved', updated_at=? WHERE record_id=?",
                     (category, now(), record_id))
        conn.execute("UPDATE flags SET status='resolved', resolved_by=?, resolved_note=? WHERE entity_type='transaction' AND entity_id=? AND code IN ('needs_classification')",
                     (user.name, note or "classified", str(record_id)))
        audit(conn, user.name, "transaction.classify", "transaction", record_id, {"category": t["category"], "status": t["classification_status"]},
              {"category": category, "status": "approved"})


# ------------------------------------------------------------- duplicates


def detect_duplicates(conn, user: User) -> dict:
    """Same account, date, amount and similar text but a *different* source reference."""
    user.require("classify")
    found = 0
    with tx(conn):
        rows = conn.execute("SELECT * FROM transactions ORDER BY record_id").fetchall()
        buckets: dict = {}
        for t in rows:
            if t["parent_record_id"]:
                continue
            buckets.setdefault((t["account_id"], t["transaction_date"], t["amount_minor"], t["currency"]), []).append(t)
        for key, grp in buckets.items():
            for i in range(1, len(grp)):
                a, b = grp[0], grp[i]
                if _sim(a["description"], b["description"]) >= 0.6 or _norm(a["counterparty"]) == _norm(b["counterparty"]) != "":
                    raise_flag(conn, "transaction", b["record_id"], "possible_duplicate", "warning",
                               f"Same account/date/amount as #{a['record_id']} ({format_minor(b['amount_minor'], b['currency'])}) but different source reference")
                    found += 1
        audit(conn, user.name, "bookkeeping.detect_duplicates", "transactions", None, None, {"flagged": found})
    return {"flagged": found}


# ------------------------------------------------------------- matching


def match_transactions(conn, user: User) -> dict:
    user.require("classify")
    confirmed = proposed = discrepancies = 0
    with tx(conn):
        txns = conn.execute("SELECT * FROM transactions WHERE reconciliation_status='unreconciled' AND status='settled' AND kind IN ('income','expense','refund')").fetchall()
        used_inv = {r[0] for r in conn.execute("SELECT doc_id FROM matches WHERE doc_type='invoice' AND status='confirmed'")}
        used_rcpt = {r[0] for r in conn.execute("SELECT doc_id FROM matches WHERE doc_type='receipt' AND status='confirmed'")}
        for t in txns:
            amt = abs(t["amount_minor"])
            direction = "receivable" if t["amount_minor"] > 0 else "payable"
            cands = []
            for inv in conn.execute("SELECT * FROM invoices WHERE direction=? AND status='open' AND currency=?", (direction, t["currency"])):
                if str(inv["id"]) in used_inv:
                    continue
                cp = _contain(inv["counterparty"], f"{t['counterparty'] or ''} {t['description']}")
                days = abs((date.fromisoformat(t["transaction_date"]) - date.fromisoformat(inv["due_date"] or inv["issue_date"])).days)
                if inv["amount_minor"] + inv["tax_minor"] == amt:
                    score = 0.5 + 0.4 * cp + (0.1 if days <= 30 else 0)
                    cands.append(("invoice", str(inv["id"]), round(score, 2), "exact amount"))
                elif cp >= 0.5 and abs((inv["amount_minor"] + inv["tax_minor"]) - amt) <= max(100, amt // 50):
                    cands.append(("invoice", str(inv["id"]), 0.55, "amount differs"))
            if t["amount_minor"] < 0:
                for r in conn.execute("SELECT * FROM receipts WHERE currency=?", (t["currency"],)):
                    if str(r["id"]) in used_rcpt:
                        continue
                    days = abs((date.fromisoformat(t["transaction_date"]) - date.fromisoformat(r["receipt_date"])).days)
                    if r["amount_minor"] == amt and days <= 7:
                        cands.append(("receipt", str(r["id"]), round(0.6 + 0.4 * _contain(r["vendor"], f"{t['counterparty'] or ''} {t['description']}"), 2), "exact amount, within 7 days"))
            cands.sort(key=lambda c: -c[2])
            if not cands:
                continue
            top = cands[0]
            unique = len(cands) == 1 or cands[0][2] - cands[1][2] >= 0.2
            if top[2] >= 0.95 and unique and top[3] == "exact amount" or (top[0] == "receipt" and top[2] >= 0.95 and unique):
                conn.execute("INSERT OR IGNORE INTO matches (transaction_id,doc_type,doc_id,score,status,reason,created_at) VALUES (?,?,?,?, 'confirmed', ?,?)",
                             (t["record_id"], top[0], top[1], top[2], f"auto: {top[3]}", now()))
                conn.execute("UPDATE transactions SET reconciliation_status='matched', updated_at=? WHERE record_id=?", (now(), t["record_id"]))
                (used_inv if top[0] == "invoice" else used_rcpt).add(top[1])
                confirmed += 1
            elif top[3] == "amount differs":
                conn.execute("INSERT OR IGNORE INTO matches (transaction_id,doc_type,doc_id,score,status,reason,created_at) VALUES (?,?,?,?, 'proposed', ?,?)",
                             (t["record_id"], top[0], top[1], top[2], top[3], now()))
                conn.execute("UPDATE transactions SET reconciliation_status='exception', updated_at=? WHERE record_id=?", (now(), t["record_id"]))
                raise_flag(conn, "transaction", t["record_id"], "amount_discrepancy", "material", f"Close counterparty match to invoice #{top[1]} but amount differs")
                discrepancies += 1
            else:
                conn.execute("INSERT OR IGNORE INTO matches (transaction_id,doc_type,doc_id,score,status,reason,created_at) VALUES (?,?,?,?, 'proposed', ?,?)",
                             (t["record_id"], top[0], top[1], top[2], f"proposed: {top[3]}", now()))
                proposed += 1
        audit(conn, user.name, "bookkeeping.match", "transactions", None, None, {"confirmed": confirmed, "proposed": proposed, "discrepancies": discrepancies})
    return {"confirmed": confirmed, "proposed": proposed, "discrepancies": discrepancies}


def confirm_match(conn, user: User, match_id: int, accept: bool):
    user.require("confirm_match")
    with tx(conn):
        m = conn.execute("SELECT * FROM matches WHERE id=? AND status='proposed'", (match_id,)).fetchone()
        if not m:
            raise ControlViolation("no such proposed match")
        conn.execute("UPDATE matches SET status=? WHERE id=?", ("confirmed" if accept else "rejected", match_id))
        if accept:
            conn.execute("UPDATE transactions SET reconciliation_status='matched', updated_at=? WHERE record_id=?", (now(), m["transaction_id"]))
            conn.execute("UPDATE flags SET status='resolved', resolved_by=?, resolved_note='match confirmed' WHERE entity_type='transaction' AND entity_id=? AND code='amount_discrepancy'",
                         (user.name, str(m["transaction_id"])))
        audit(conn, user.name, "match.confirm" if accept else "match.reject", "match", match_id)


# ------------------------------------------------------------- flags / scans


def scan_flags(conn, user: User) -> dict:
    """Missing receipts, unexplained discrepancies, unusual spend."""
    user.require("classify")
    thr = int(setting(conn, "missing_receipt_threshold_minor"))
    mult = float(setting(conn, "unusual_spend_multiplier"))
    n = {"missing_receipt": 0, "unusual_spend": 0}
    with tx(conn):
        for t in conn.execute("SELECT * FROM transactions WHERE kind='expense' AND status='settled' AND -amount_minor>=?", (thr,)):
            has = conn.execute("SELECT 1 FROM matches WHERE transaction_id=? AND status='confirmed'", (t["record_id"],)).fetchone()
            if not has:
                raise_flag(conn, "transaction", t["record_id"], "missing_receipt", "warning", "Expense has no confirmed receipt or invoice match")
                n["missing_receipt"] += 1
        # unusual: expense > mult x the average of prior expenses with same counterparty (needs >=3 prior)
        for t in conn.execute("SELECT * FROM transactions WHERE kind='expense' AND counterparty IS NOT NULL").fetchall():
            prior = [r[0] for r in conn.execute("SELECT -amount_minor FROM transactions WHERE kind='expense' AND counterparty=? AND transaction_date<? AND currency=?",
                                                (t["counterparty"], t["transaction_date"], t["currency"]))]
            if len(prior) >= 3 and -t["amount_minor"] > mult * (sum(prior) / len(prior)):
                raise_flag(conn, "transaction", t["record_id"], "unusual_spend", "warning",
                           f"{format_minor(-t['amount_minor'], t['currency'])} vs prior average {format_minor(sum(prior)//len(prior), t['currency'])}")
                n["unusual_spend"] += 1
        # webhook says succeeded but no settlement after 7 days
        for e in conn.execute("SELECT * FROM payment_events WHERE settlement_status='unsettled' AND julianday('now')-julianday(received_at)>7"):
            raise_flag(conn, "payment_event", e["event_id"], "event_without_settlement", "material", "Payment event has no settled balance transaction after 7 days")
        audit(conn, user.name, "bookkeeping.scan_flags", None, None, None, n)
    return n


def resolve_flag(conn, user: User, flag_id: int, note: str, dismiss=False):
    user.require("resolve_flag")
    with tx(conn):
        f = conn.execute("SELECT * FROM flags WHERE id=? AND status='open'", (flag_id,)).fetchone()
        if not f:
            raise ControlViolation("no such open flag")
        if f["severity"] == "material" and not user.can("approve"):
            raise ControlViolation("material flags can only be resolved by an approver")
        if not note.strip():
            raise ControlViolation("a resolution note is required")
        conn.execute("UPDATE flags SET status=?, resolved_by=?, resolved_note=? WHERE id=?", ("dismissed" if dismiss else "resolved", user.name, note, flag_id))
        audit(conn, user.name, "flag.resolve", "flag", flag_id, {"status": "open"}, {"note": note})


# ------------------------------------------------------------- proposed entries


def propose_entries(conn, user: User) -> dict:
    """Draft journals for matched/approved transactions. Revenue is NOT recognised from cash receipts."""
    user.require("draft_journal")
    from ..core import create_entry
    drafted = skipped = 0
    for t in conn.execute("""SELECT t.*, a.funds_class FROM transactions t JOIN accounts a ON a.id=t.account_id
                             WHERE t.status='settled' AND t.classification_status='approved'
                             AND NOT EXISTS (SELECT 1 FROM journal_entries j WHERE j.source_ref='txn:'||t.record_id)""").fetchall():
        amt = abs(t["amount_minor"])
        cash = "1050" if t["funds_class"] != "operating" else "1000"
        ref = f"txn:{t['record_id']}"
        inv = conn.execute("SELECT m.doc_type FROM matches m WHERE transaction_id=? AND status='confirmed'", (t["record_id"],)).fetchone()
        if t["kind"] == "income":
            if t["funds_class"] == "customer_funds":
                lines = [(cash, amt, 0), ("2100", 0, amt)]
            elif inv and inv["doc_type"] == "invoice":
                lines = [(cash, amt, 0), ("1100", 0, amt)]
            else:  # cash received with no invoice: suspense, not revenue
                lines = [(cash, amt, 0), ("2400", 0, amt)]
        elif t["kind"] == "fee":
            lines = [("5000", amt, 0), (cash if t["funds_class"] == "operating" else "1010", 0, amt)]
        elif t["kind"] == "refund":
            lines = [("4090", amt, 0), (cash, 0, amt)]
        elif t["kind"] == "expense":
            if inv and inv["doc_type"] == "invoice":
                lines = [("2000", amt, 0), (cash, 0, amt)]
            else:
                lines = [(t["category"], amt, 0), (cash, 0, amt)]
        else:
            skipped += 1  # transfers need a counter-account decision by a human
            raise_flag(conn, "transaction", t["record_id"], "needs_transfer_counterpart", "info", "Transfer needs a counter-account before it can be journalled")
            continue
        create_entry(conn, user, t["transaction_date"], f"{t['kind']}: {t['description'][:60]}", lines, t["currency"], source_ref=ref)
        drafted += 1
    return {"drafted": drafted, "skipped": skipped}


def propose_invoice_entries(conn, user: User) -> dict:
    """Receivable invoice -> Dr AR / Cr revenue (+GST only if registration is verified). Payable -> Dr expense / Cr AP."""
    user.require("draft_journal")
    from ..core import create_entry
    fact = conn.execute("SELECT value,verified FROM company_facts WHERE key='gst_registered'").fetchone()
    gst_registered = bool(fact and fact["verified"] and (fact["value"] or "").lower() in ("true", "yes", "1"))
    drafts = []
    with tx(conn):
        for inv in conn.execute("SELECT * FROM invoices WHERE approval_status='approved' AND status IN ('open','paid')").fetchall():
            if inv["tax_minor"] and not gst_registered:
                raise_flag(conn, "invoice", inv["id"], "tax_without_verified_registration", "material",
                           "Invoice carries GST/tax but GST registration is not verified; not journalled until resolved")
                continue
            if conn.execute("SELECT 1 FROM journal_entries WHERE source_ref=?", (f"inv:{inv['id']}",)).fetchone():
                continue
            net, tax = inv["amount_minor"], inv["tax_minor"]
            total = net + tax
            if inv["direction"] == "receivable":
                rev = "4100" if (inv["product"] or "").lower().startswith(("visib", "promo")) else "4000"
                lines = [("1100", total, 0), (rev, 0, net)] + ([("2200", 0, tax)] if tax else [])
            else:
                lines = [("6900", net, 0)] + ([("2200", tax, 0)] if tax else []) + [("2000", 0, total)]
            drafts.append((inv, lines))
    for inv, lines in drafts:
        create_entry(conn, user, inv["issue_date"], f"{inv['direction']} invoice {inv['number'] or inv['id']} - {inv['counterparty']}",
                     lines, inv["currency"], source_ref=f"inv:{inv['id']}")
    return {"drafted": len(drafts)}


# ------------------------------------------------------------- reconciliation report


def reconciliation_report(conn, user: User, account_id: int, period: str) -> dict:
    """Marks 'complete' ONLY when verified closing evidence exists, the numbers agree, and nothing is open."""
    user.require("classify")
    acct = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    start, end = month_bounds(period)
    opening = conn.execute("SELECT * FROM balance_snapshots WHERE account_id=? AND verified=1 AND as_of<? ORDER BY as_of DESC LIMIT 1", (account_id, start)).fetchone()
    closing = conn.execute("SELECT * FROM balance_snapshots WHERE account_id=? AND verified=1 AND as_of>=? AND as_of<=? ORDER BY as_of DESC LIMIT 1", (account_id, f"{period}-28", end)).fetchone()
    mov = conn.execute("SELECT COALESCE(SUM(amount_minor),0) FROM transactions WHERE account_id=? AND transaction_date>=? AND transaction_date<=? AND status='settled'", (account_id, start, end)).fetchone()[0]
    open_flags = conn.execute("""SELECT COUNT(*) FROM flags f JOIN transactions t ON f.entity_type='transaction' AND f.entity_id=CAST(t.record_id AS TEXT)
                                 WHERE f.status='open' AND t.account_id=? AND t.accounting_period=?""", (account_id, period)).fetchone()[0]
    unreconciled = conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id=? AND accounting_period=? AND reconciliation_status IN ('unreconciled','exception') AND status='settled'", (account_id, period)).fetchone()[0]
    reasons = []
    exp = diff = None
    if not opening:
        reasons.append("no verified opening balance")
    if not closing:
        reasons.append("no verified statement closing balance")
    if opening and closing:
        exp = opening["amount_minor"] + mov
        diff = closing["amount_minor"] - exp
        if diff != 0:
            reasons.append(f"unexplained difference {format_minor(diff, acct['currency'])}")
    if open_flags:
        reasons.append(f"{open_flags} open flag(s)")
    if unreconciled:
        reasons.append(f"{unreconciled} transaction(s) not matched to a document")
    status = "complete" if not reasons else "incomplete"
    ev = {"opening_snapshot": opening["id"] if opening else None, "closing_snapshot": closing["id"] if closing else None}
    with tx(conn):
        cur = conn.execute("INSERT INTO reconciliations (account_id,period,opening_minor,movements_minor,expected_closing_minor,statement_closing_minor,difference_minor,open_flags,status,reasons_json,evidence_json,created_by,created_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (account_id, period, opening["amount_minor"] if opening else None, mov, exp, closing["amount_minor"] if closing else None, diff, open_flags,
                            status, json.dumps(reasons), json.dumps(ev), user.name, now()))
        audit(conn, user.name, "reconciliation.report", "reconciliation", cur.lastrowid, None, {"status": status, "reasons": reasons})
    return {"id": cur.lastrowid, "account": acct["name"], "period": period, "status": status, "reasons": reasons, "opening_minor": opening["amount_minor"] if opening else None,
            "movements_minor": mov, "expected_closing_minor": exp, "statement_closing_minor": closing["amount_minor"] if closing else None, "difference_minor": diff,
            "unreconciled": unreconciled, "open_flags": open_flags, "currency": acct["currency"], "evidence": ev}
