"""Audit trail, RBAC, approvals and the journal engine."""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass

from .db import ensure_period, now, setting

# --------------------------------------------------------------------------- audit


def _canon(x) -> str:
    return json.dumps(x, sort_keys=True, separators=(",", ":"), default=str)


def audit(conn, actor, action, entity=None, entity_id=None, before=None, after=None):
    row = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev = row[0] if row else "GENESIS"
    ts = now()
    body = _canon([ts, actor, action, entity, str(entity_id) if entity_id is not None else None, before, after])
    h = hashlib.sha256((prev + body).encode()).hexdigest()
    conn.execute(
        "INSERT INTO audit_log (ts,actor,action,entity,entity_id,before_json,after_json,prev_hash,hash) VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, actor, action, entity, str(entity_id) if entity_id is not None else None,
         _canon(before) if before is not None else None, _canon(after) if after is not None else None, prev, h))


def verify_audit_chain(conn):
    prev = "GENESIS"
    for r in conn.execute("SELECT * FROM audit_log ORDER BY id"):
        before = json.loads(r["before_json"]) if r["before_json"] else None
        after = json.loads(r["after_json"]) if r["after_json"] else None
        body = _canon([r["ts"], r["actor"], r["action"], r["entity"], r["entity_id"], before, after])
        if r["prev_hash"] != prev or hashlib.sha256((prev + body).encode()).hexdigest() != r["hash"]:
            return False, r["id"]
        prev = r["hash"]
    return True, None


class tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK context manager (connection is in autocommit mode)."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, et, ev, tb):
        self.conn.execute("ROLLBACK" if et else "COMMIT")
        return False


# --------------------------------------------------------------------------- RBAC

PERMS = {
    "viewer": {"read"},
    "preparer": {"read", "import", "classify", "draft_journal", "enter_docs", "run_tasks", "plan", "request_approval", "resolve_flag"},
}
PERMS["approver"] = PERMS["preparer"] | {"approve", "post_journal", "close_period", "approve_report", "complete_reconciliation", "confirm_match"}
PERMS["finance_admin"] = PERMS["approver"] | {"manage_users", "settings", "connectors"}


class Forbidden(PermissionError):
    pass


class ControlViolation(ValueError):
    """A financial control prevented the action (separation of duties, threshold, policy)."""


@dataclass
class User:
    id: int
    name: str
    role: str

    def can(self, perm: str) -> bool:
        return perm in PERMS.get(self.role, set())

    def require(self, perm: str):
        if not self.can(perm):
            raise Forbidden(f"{self.name} ({self.role}) lacks permission '{perm}'")


SYSTEM = User(0, "system", "preparer")  # automation can prepare, never approve/post


def _hash_token(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def create_user(conn, name, role, actor="bootstrap") -> str:
    token = "kff_" + secrets.token_urlsafe(32)
    with tx(conn):
        conn.execute("INSERT INTO users (name,role,token_hash,created_at) VALUES (?,?,?,?)", (name, role, _hash_token(token), now()))
        audit(conn, actor, "user.create", "user", name, None, {"role": role})
    return token


def authenticate(conn, token: str | None) -> User | None:
    if not token:
        return None
    r = conn.execute("SELECT id,name,role FROM users WHERE token_hash=? AND active=1", (_hash_token(token),)).fetchone()
    return User(r["id"], r["name"], r["role"]) if r else None


# --------------------------------------------------------------------------- approvals

NEVER_AUTOMATED = {"payment", "tax_submission", "export_external", "permission_change", "settings_change", "vendor_change", "expenditure"}


def request_approval(conn, user: User, type_: str, payload: dict, amount_minor=None, currency=None, idem=None) -> int:
    user.require("request_approval")
    with tx(conn):
        if idem:
            ex = conn.execute("SELECT id FROM approvals WHERE idempotency_key=?", (idem,)).fetchone()
            if ex:
                return ex[0]
        cur = conn.execute(
            "INSERT INTO approvals (type,payload_json,amount_minor,currency,requested_by,idempotency_key,created_at) VALUES (?,?,?,?,?,?,?)",
            (type_, _canon(payload), amount_minor, currency, user.name, idem, now()))
        audit(conn, user.name, "approval.request", "approval", cur.lastrowid, None, {"type": type_, "amount_minor": amount_minor})
        return cur.lastrowid


def decide_approval(conn, user: User, approval_id: int, approve: bool, note: str = "") -> dict:
    user.require("approve")
    with tx(conn):
        a = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not a:
            raise ControlViolation("approval not found")
        if a["status"] != "pending":
            raise ControlViolation(f"approval already {a['status']}")
        if setting(conn, "enforce_separation_of_duties") == "1" and a["requested_by"] == user.name:
            raise ControlViolation("separation of duties: requester cannot decide their own request")
        payload = json.loads(a["payload_json"])
        if approve and a["type"] == "tax_submission" and not payload.get("professional_review_completed"):
            raise ControlViolation("tax submissions require recorded professional review before approval")
        if approve and a["type"] in ("permission_change", "settings_change") and user.role != "finance_admin":
            raise ControlViolation("permission changes need a finance_admin")
        result = {}
        status = "approved" if approve else "rejected"
        if approve:
            result = _EFFECTS.get(a["type"], lambda c, u, p: {})(conn, user, payload) or {}
        elif a["type"] in _REJECT_EFFECTS:
            _REJECT_EFFECTS[a["type"]](conn, user, payload)
        conn.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                     (status, user.name, now(), note, approval_id))
        audit(conn, user.name, f"approval.{status}", "approval", approval_id, {"status": "pending"}, {"status": status, "type": a["type"]})
        return {"id": approval_id, "status": status, "type": a["type"], "result": result,
                "note": "Approval authorises a human to act; the system never moves money or files returns itself."
                if a["type"] in NEVER_AUTOMATED else ""}


def _effect_permission_change(conn, user, p):
    if p["action"] == "set_role":
        conn.execute("UPDATE users SET role=? WHERE name=?", (p["role"], p["name"]))
        return {"user": p["name"], "role": p["role"]}
    if p["action"] == "create_user":
        token = "kff_" + secrets.token_urlsafe(32)
        conn.execute("INSERT INTO users (name,role,token_hash,created_at) VALUES (?,?,?,?)", (p["name"], p["role"], _hash_token(token), now()))
        return {"user": p["name"], "role": p["role"], "token_shown_once": token}
    if p["action"] == "deactivate":
        conn.execute("UPDATE users SET active=0 WHERE name=?", (p["name"],))
        return {"user": p["name"], "active": False}
    raise ControlViolation("unknown permission action")


def _num(v, lo=None, hi=None, integer=False):
    try:
        x = int(v) if integer else float(v)
    except (TypeError, ValueError):
        raise ControlViolation(f"{v!r} is not a valid {'integer' if integer else 'number'}") from None
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        raise ControlViolation(f"{v!r} is outside the allowed range {lo}..{hi}")
    return str(x)


SETTING_RULES = {  # the only settings that may be changed, and how each is validated
    "approval_threshold_minor": lambda v: _num(v, 0, integer=True),
    "material_adjustment_minor": lambda v: _num(v, 0, integer=True),
    "missing_receipt_threshold_minor": lambda v: _num(v, 0, integer=True),
    "unusual_spend_multiplier": lambda v: _num(v, 1.1, 100),
    "ar_collection_rate": lambda v: _num(v, 0, 1),
    "ar_collection_delay_days": lambda v: _num(v, 0, 180, integer=True),
    "auto_approve_classification": lambda v: _num(v, 0, 1, integer=True),
    "enforce_separation_of_duties": lambda v: _num(v, 0, 1, integer=True),
    "coa_approved_by_accountant": lambda v: _num(v, 0, 1, integer=True),
}


def validate_setting(key, value) -> str:
    if key not in SETTING_RULES:
        raise ControlViolation(f"setting {key!r} cannot be changed here (allowed: {sorted(SETTING_RULES)})")
    return SETTING_RULES[key](value)


def _effect_settings_change(conn, user, p):
    value = validate_setting(p["key"], p["value"])
    old = conn.execute("SELECT value FROM settings WHERE key=?", (p["key"],)).fetchone()
    conn.execute("UPDATE settings SET value=?, updated_at=? WHERE key=?", (value, now(), p["key"]))
    audit(conn, user.name, "settings.change", "setting", p["key"], {"value": old[0] if old else None}, {"value": value})
    return {"key": p["key"], "old": old[0] if old else None, "new": value}


def _effect_period_close(conn, user, p):
    conn.execute("UPDATE periods SET status='closed', closed_by=?, closed_at=? WHERE id=?", (user.name, now(), p["period"]))
    return {"period": p["period"], "status": "closed"}


def _effect_invoice(conn, user, p):
    conn.execute("UPDATE invoices SET approval_status='approved', updated_at=? WHERE id=?", (now(), p["invoice_id"]))
    return {"invoice_id": p["invoice_id"], "approval_status": "approved"}


def _effect_vendor_change(conn, user, p):
    conn.execute("INSERT INTO vendors (name,bank_details_ref,created_at) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET bank_details_ref=excluded.bank_details_ref",
                 (p["vendor"], p["new_details_ref"], now()))
    return {"vendor": p["vendor"], "bank_details_ref_updated": True}


def _effect_classification_review(conn, user, p):
    t = conn.execute("SELECT * FROM transactions WHERE record_id=?", (p["record_id"],)).fetchone()
    if not t or t["classification_status"] == "approved":
        return {"record_id": p["record_id"], "changed": False}
    conn.execute("UPDATE transactions SET category=?, classification_status='approved', approval_status='approved', updated_at=? WHERE record_id=?",
                 (p["category"], now(), p["record_id"]))
    conn.execute("UPDATE flags SET status='resolved', resolved_by=?, resolved_note='classification approved' WHERE entity_type='transaction' AND entity_id=? AND code='needs_classification'",
                 (user.name, str(p["record_id"])))
    return {"record_id": p["record_id"], "category": p["category"], "changed": True}


def _reject_invoice(conn, user, p):
    conn.execute("UPDATE invoices SET approval_status='rejected', updated_at=? WHERE id=?", (now(), p["invoice_id"]))


_EFFECTS = {"permission_change": _effect_permission_change, "period_close": _effect_period_close,
            "invoice": _effect_invoice, "vendor_change": _effect_vendor_change,
            "classification_review": _effect_classification_review, "settings_change": _effect_settings_change}
_REJECT_EFFECTS = {"invoice": _reject_invoice}


def require_approved(conn, approval_id, type_):
    a = conn.execute("SELECT * FROM approvals WHERE id=? AND type=? AND status='approved'", (approval_id, type_)).fetchone()
    if not a:
        raise ControlViolation(f"requires an approved '{type_}' approval")
    return a


# --------------------------------------------------------------------------- journal engine


def create_entry(conn, user: User, entry_date: str, memo: str, lines: list[tuple], currency: str,
                 kind="standard", source_ref=None, reverses_id=None) -> int:
    """lines: [(account_code, debit_minor, credit_minor)]. Created as a draft; posting enforces balance."""
    user.require("draft_journal")
    with tx(conn):
        period = ensure_period(conn, entry_date)
        cur = conn.execute(
            "INSERT INTO journal_entries (entry_date,period,memo,currency,kind,source_ref,reverses_id,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (entry_date, period, memo, currency, kind, source_ref, reverses_id, user.name, now()))
        eid = cur.lastrowid
        for code, dr, cr in lines:
            conn.execute("INSERT INTO journal_lines (entry_id,account_code,debit_minor,credit_minor,currency) VALUES (?,?,?,?,?)",
                         (eid, code, dr, cr, currency))
        audit(conn, user.name, "journal.draft", "journal_entry", eid, None, {"memo": memo, "lines": lines, "currency": currency})
        return eid


def post_entry(conn, user: User, entry_id: int, approval_id: int | None = None):
    user.require("post_journal")
    with tx(conn):
        e = conn.execute("SELECT * FROM journal_entries WHERE id=?", (entry_id,)).fetchone()
        if not e:
            raise ControlViolation("entry not found")
        if e["status"] != "draft":
            raise ControlViolation(f"entry is {e['status']}")
        if setting(conn, "enforce_separation_of_duties") == "1" and e["created_by"] == user.name:
            raise ControlViolation("separation of duties: creator cannot post their own entry")
        total = conn.execute("SELECT COALESCE(SUM(debit_minor),0) FROM journal_lines WHERE entry_id=?", (entry_id,)).fetchone()[0]
        if e["kind"] == "adjustment" and total >= int(setting(conn, "material_adjustment_minor")):
            if approval_id is None:
                raise ControlViolation("material adjustment requires an approved 'material_adjustment' approval")
            require_approved(conn, approval_id, "material_adjustment")
        try:
            conn.execute("UPDATE journal_entries SET status='posted', posted_by=?, posted_at=? WHERE id=?", (user.name, now(), entry_id))
        except Exception as ex:  # trigger aborts: unbalanced, closed period, ...
            raise ControlViolation(str(ex)) from None
        audit(conn, user.name, "journal.post", "journal_entry", entry_id, {"status": "draft"}, {"status": "posted", "total_minor": total})


def reverse_entry(conn, user: User, entry_id: int, reversal_date: str, reason: str) -> int:
    """Controlled correction: original is marked reversed; a mirror entry is drafted in the period of reversal_date."""
    user.require("draft_journal")
    with tx(conn):
        e = conn.execute("SELECT * FROM journal_entries WHERE id=?", (entry_id,)).fetchone()
        if not e or e["status"] != "posted":
            raise ControlViolation("only posted entries can be reversed")
        if conn.execute("SELECT 1 FROM journal_entries WHERE reverses_id=?", (entry_id,)).fetchone():
            raise ControlViolation("entry already has a reversal")
        lines = conn.execute("SELECT account_code,debit_minor,credit_minor FROM journal_lines WHERE entry_id=?", (entry_id,)).fetchall()
        period = ensure_period(conn, reversal_date)
        cur = conn.execute(
            "INSERT INTO journal_entries (entry_date,period,memo,currency,kind,reverses_id,created_by,created_at) VALUES (?,?,?,?, 'reversal', ?,?,?)",
            (reversal_date, period, f"Reversal of #{entry_id}: {reason}", e["currency"], entry_id, user.name, now()))
        rid = cur.lastrowid
        for l in lines:
            conn.execute("INSERT INTO journal_lines (entry_id,account_code,debit_minor,credit_minor,currency) VALUES (?,?,?,?,?)",
                         (rid, l["account_code"], l["credit_minor"], l["debit_minor"], e["currency"]))
        audit(conn, user.name, "journal.reversal_drafted", "journal_entry", rid, None, {"reverses": entry_id, "reason": reason})
        return rid


def finalize_reversal(conn, user: User, reversal_id: int):
    """Poster approves the reversal; the original is then marked reversed (only permitted status transition)."""
    user.require("post_journal")
    with tx(conn):
        r = conn.execute("SELECT * FROM journal_entries WHERE id=? AND kind='reversal'", (reversal_id,)).fetchone()
        if not r or r["status"] != "draft":
            raise ControlViolation("not a draft reversal")
        if setting(conn, "enforce_separation_of_duties") == "1" and r["created_by"] == user.name:
            raise ControlViolation("separation of duties: creator cannot post their own entry")
        try:
            conn.execute("UPDATE journal_entries SET status='posted', posted_by=?, posted_at=? WHERE id=?", (user.name, now(), reversal_id))
        except Exception as ex:
            raise ControlViolation(str(ex)) from None
        conn.execute("UPDATE journal_entries SET status='reversed' WHERE id=?", (r["reverses_id"],))
        audit(conn, user.name, "journal.reversal_posted", "journal_entry", reversal_id, None, {"reverses": r["reverses_id"]})


def trial_balance(conn, up_to: str | None = None, period_from: str | None = None):
    q = ("SELECT l.account_code code, c.name, c.type, l.currency, SUM(l.debit_minor) dr, SUM(l.credit_minor) cr "
         "FROM journal_lines l JOIN journal_entries e ON e.id=l.entry_id JOIN coa c ON c.code=l.account_code "
         "WHERE e.status IN ('posted','reversed')")
    args = []
    if up_to:
        q += " AND e.entry_date<=?"; args.append(up_to)
    if period_from:
        q += " AND e.entry_date>=?"; args.append(period_from)
    q += " GROUP BY l.account_code, l.currency ORDER BY l.account_code"
    return [dict(r) for r in conn.execute(q, args)]
