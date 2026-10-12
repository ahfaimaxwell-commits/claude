"""Transaction ingestion: validated, idempotent, every row traceable to a source record."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import re
import time
from datetime import date, datetime, timezone

from .core import ControlViolation, User, audit, tx
from .db import ensure_period, now
from .money import MoneyError, check_currency, exponent, parse_amount

DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y")
REQUIRED_COLS = ("date", "description", "amount")


def parse_date(s: str) -> str:
    s = (s or "").strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {s!r}")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _kind_from_amount(minor: int) -> str:
    return "income" if minor > 0 else "expense"


def _insert_source(conn, source_key, source_type, batch_id, raw: dict):
    raw_json = json.dumps(raw, sort_keys=True)
    cur = conn.execute("INSERT OR IGNORE INTO source_records (source_key,source_type,batch_id,raw_json,content_sha256,imported_at) VALUES (?,?,?,?,?,?)",
                       (source_key, source_type, batch_id, raw_json, sha256(raw_json), now()))
    return cur.lastrowid if cur.rowcount else None


def _insert_txn(conn, source_id, account, date_, minor, currency, kind, desc, counterparty, status="settled", parent=None):
    period = ensure_period(conn, date_)
    cur = conn.execute(
        "INSERT INTO transactions (source_id,account_id,transaction_date,amount_minor,currency,kind,counterparty,description,status,"
        "accounting_period,parent_record_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (source_id, account["id"], date_, minor, currency, kind, counterparty, desc, status, period, parent, now(), now()))
    return cur.lastrowid


def _begin_batch(conn, user, source_type, filename, account_id, content_hash):
    """Returns batch id, or None if this exact file/payload was already imported (no changes made)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO import_batches (source_type,filename,account_id,content_sha256,status,imported_by,imported_at) VALUES (?,?,?,?, 'running', ?,?)",
        (source_type, filename, account_id, content_hash, user.name, now()))
    return cur.lastrowid if cur.rowcount else None


def import_csv(conn, user: User, account_id: int, content: str, filename: str = "upload.csv") -> dict:
    user.require("import")
    acct = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not acct:
        raise ControlViolation("unknown account")
    h = sha256(f"csv:{account_id}:{content}")
    with tx(conn):
        batch = _begin_batch(conn, user, "csv", filename, account_id, h)
        if batch is None:
            audit(conn, user.name, "import.duplicate_file", "import_batch", None, None, {"filename": filename, "sha256": h})
            return {"status": "duplicate_file", "imported": 0, "message": "This exact file was already imported; nothing changed."}
        reader = csv.DictReader(io.StringIO(content))
        cols = {(c or "").strip().lower(): c for c in (reader.fieldnames or [])}
        missing = [c for c in REQUIRED_COLS if c not in cols]
        if missing:
            raise ControlViolation(f"CSV missing required columns: {missing}")
        imported = dup = rejected = total = 0
        seen_in_file: dict[str, int] = {}
        for n, row in enumerate(reader, start=2):
            total += 1
            r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            try:
                ccy = (r.get("currency") or acct["currency"]).upper()
                check_currency(ccy)
                if ccy != acct["currency"]:
                    raise ValueError(f"currency {ccy} differs from account currency {acct['currency']}")
                d = parse_date(r["date"])
                minor = parse_amount(r["amount"], ccy)
                if minor == 0:
                    raise ValueError("zero amount")
                if not r["description"]:
                    raise ValueError("empty description")
            except (ValueError, MoneyError) as ex:
                rejected += 1
                conn.execute("INSERT INTO import_rejects (batch_id,row_number,reason,raw) VALUES (?,?,?,?)", (batch, n, str(ex), json.dumps(r)))
                continue
            ref = r.get("reference") or r.get("id")
            if not ref:  # derive a stable key; identical rows in one file are distinct occurrences
                base = sha256(f"{account_id}|{d}|{minor}|{_norm(r['description'])}")[:24]
                seen_in_file[base] = seen_in_file.get(base, 0) + 1
                ref = f"{base}#{seen_in_file[base]}"
            sid = _insert_source(conn, f"csv:{account_id}:{ref}", "csv", batch, {"row": n, **r})
            if sid is None:
                dup += 1
                continue
            _insert_txn(conn, sid, acct, d, minor, ccy, _kind_from_amount(minor), r["description"], r.get("counterparty") or None)
            imported += 1
        conn.execute("UPDATE import_batches SET status='complete', rows_total=?, rows_imported=?, rows_duplicate_source=?, rows_rejected=? WHERE id=?",
                     (total, imported, dup, rejected, batch))
        audit(conn, user.name, "import.csv", "import_batch", batch, None,
              {"filename": filename, "total": total, "imported": imported, "duplicate_source": dup, "rejected": rejected})
        _touch_connector(conn, "bank_csv", ok=True)
    return {"status": "complete", "batch_id": batch, "total": total, "imported": imported, "duplicate_source": dup, "rejected": rejected}


# Stripe balance_transaction types -> (kind, status handling)
_STRIPE_KIND = {
    "charge": "income", "payment": "income", "payment_refund": "refund", "refund": "refund",
    "payout": "transfer", "payout_cancel": "transfer", "payout_failure": "transfer",
    "stripe_fee": "fee", "application_fee": "fee", "adjustment": "other", "dispute": "refund", "dispute_reversal": "other",
}


def import_stripe_balance_transactions(conn, user: User, account_id: int, bts: list[dict], filename="stripe_balance_transactions.json", now_epoch=None) -> dict:
    """Import Stripe balance transactions (the authoritative settlement ledger), not webhook events."""
    user.require("import")
    acct = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not acct:
        raise ControlViolation("unknown account")
    now_epoch = now_epoch or int(time.time())
    payload = json.dumps(bts, sort_keys=True)
    h = sha256(f"stripe:{account_id}:{payload}")
    with tx(conn):
        batch = _begin_batch(conn, user, "stripe", filename, account_id, h)
        if batch is None:
            audit(conn, user.name, "import.duplicate_file", "import_batch", None, None, {"filename": filename, "sha256": h})
            return {"status": "duplicate_file", "imported": 0, "message": "This exact payload was already imported; nothing changed."}
        imported = dup = rejected = 0
        for i, bt in enumerate(bts):
            try:
                bid = bt["id"]
                ccy = str(bt["currency"]).upper()
                check_currency(ccy)
                if ccy != acct["currency"]:
                    raise ValueError(f"currency {ccy} differs from account currency {acct['currency']}")
                amount, fee = int(bt["amount"]), int(bt.get("fee") or 0)
                d = datetime.fromtimestamp(int(bt["created"]), timezone.utc).date().isoformat()
                typ = bt["type"]
                kind = _STRIPE_KIND.get(typ, "other")
                avail = int(bt.get("available_on") or bt["created"])
                status = "settled" if avail <= now_epoch else "pending"
                if amount == 0 and fee == 0:
                    raise ValueError("zero amount")
            except (KeyError, ValueError, TypeError, MoneyError) as ex:
                rejected += 1
                conn.execute("INSERT INTO import_rejects (batch_id,row_number,reason,raw) VALUES (?,?,?,?)", (batch, i, str(ex), json.dumps(bt)[:2000]))
                continue
            sid = _insert_source(conn, f"stripe:{account_id}:{bid}", "stripe", batch, bt)
            if sid is None:
                dup += 1
                continue
            desc = bt.get("description") or f"Stripe {typ}"
            parent = _insert_txn(conn, sid, acct, d, amount, ccy, kind, desc, "Stripe", status)
            imported += 1
            if fee > 0:  # processor fee is its own cost line, linked to the gross transaction
                fsid = _insert_source(conn, f"stripe:{account_id}:{bid}:fee", "stripe", batch, {"fee_of": bid, "fee": fee})
                if fsid:
                    _insert_txn(conn, fsid, acct, d, -fee, ccy, "fee", f"Stripe processing fee for {bid}", "Stripe", status, parent)
                    imported += 1
            # a settled balance transaction settles any matching webhook event
            src = bt.get("source")
            if src:
                conn.execute("UPDATE payment_events SET settlement_status='settled' WHERE object_id=? AND settlement_status='unsettled'", (src,))
        conn.execute("UPDATE import_batches SET status='complete', rows_total=?, rows_imported=?, rows_duplicate_source=?, rows_rejected=? WHERE id=?",
                     (len(bts), imported, dup, rejected, batch))
        audit(conn, user.name, "import.stripe", "import_batch", batch, None, {"total": len(bts), "imported": imported, "duplicate_source": dup, "rejected": rejected})
        _touch_connector(conn, "stripe", ok=True, verified=False)
    return {"status": "complete", "batch_id": batch, "total": len(bts), "imported": imported, "duplicate_source": dup, "rejected": rejected}


def verify_stripe_signature(payload: bytes, header: str, secret: str, tolerance=300, now_epoch=None) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts, sig = int(parts["t"]), parts["v1"]
    except (ValueError, KeyError, AttributeError):
        return False
    if abs((now_epoch or time.time()) - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def record_payment_event(conn, event: dict, signature_verified: bool) -> dict:
    """Record a webhook as a *source event*. It never creates a transaction or revenue by itself."""
    if not signature_verified:
        raise ControlViolation("unverified webhook rejected")
    eid = event["id"]
    obj = (event.get("data") or {}).get("object") or {}
    typ = event.get("type", "")
    settle = "failed" if typ.endswith("failed") else ("not_applicable" if not typ.startswith(("payment_intent.", "charge.")) else "unsettled")
    with tx(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO payment_events (event_id,provider,event_type,livemode,object_id,amount_minor,currency,signature_verified,payload_json,received_at,settlement_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (eid, "stripe", typ, int(bool(event.get("livemode"))), obj.get("id"), obj.get("amount"),
             (obj.get("currency") or "").upper() or None, 1, json.dumps(event, sort_keys=True), now(), settle))
        if cur.rowcount:
            audit(conn, "stripe-webhook", "payment_event.recorded", "payment_event", eid, None, {"type": typ})
    return {"recorded": bool(cur.rowcount), "duplicate": not cur.rowcount}


def _touch_connector(conn, name, ok, verified=False, error=None):
    if ok:
        conn.execute("UPDATE connectors SET last_attempt_at=?, last_success_at=?, last_error=NULL, status=CASE WHEN ? THEN 'verified' ELSE status END WHERE name=?",
                     (now(), now(), int(verified), name))
    else:
        conn.execute("UPDATE connectors SET last_attempt_at=?, last_error=? WHERE name=?", (now(), error, name))


# ----------------------------------------------------------------- documents


_AMT = re.compile(r"(?:total|amount due|grand total)\D{0,12}([\d,]+\.\d{2})", re.I)
_TAX = re.compile(r"(?:gst|tax|vat)\D{0,12}([\d,]+\.\d{2})", re.I)
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})")
_CCY = re.compile(r"\b(SGD|USD|EUR|GBP|AUD|MYR|IDR|JPY)\b")
_TERMS = re.compile(r"(net\s*\d+|due on receipt|\d+\s*days)", re.I)


def extract_document_fields(text: str) -> dict:
    """Deterministic extraction. Missing fields are returned as None for human review, never guessed."""
    amt, tax, d, ccy, terms = _AMT.search(text), _TAX.search(text), _DATE.search(text), _CCY.search(text), _TERMS.search(text)
    vendor = next((ln.strip() for ln in text.splitlines() if ln.strip()), None)
    currency = ccy.group(1) if ccy else None
    out = {"vendor": vendor, "currency": currency, "date": None, "amount": None, "tax": None, "payment_terms": terms.group(1) if terms else None}
    if d:
        try:
            out["date"] = parse_date(d.group(1))
        except ValueError:
            pass
    if currency and amt:
        out["amount"] = parse_amount(amt.group(1), currency)
    if currency and tax:
        out["tax"] = parse_amount(tax.group(1), currency)
    out["complete"] = all(out[k] is not None for k in ("vendor", "currency", "date", "amount"))
    return out
