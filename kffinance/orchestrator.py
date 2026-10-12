"""Central finance orchestrator: task queue, dependencies, retries, validation, escalation and consolidated summary."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone

from .agents import bookkeeping, fpa, reporting, revenue, tax, treasury
from .core import SYSTEM, ControlViolation, Forbidden, User, audit, request_approval, tx, verify_audit_chain
from .db import now, setting
from .ingest import import_csv, import_stripe_balance_transactions
from .money import MoneyError, check_currency


class TransientError(Exception):
    """Retryable failure (network, rate limit, lock)."""


class ValidationFailure(ValueError):
    pass


def _iso_ts(offset_s=0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _user_for(conn, name: str) -> User:
    if name == "system":
        return SYSTEM
    r = conn.execute("SELECT id,name,role FROM users WHERE name=? AND active=1", (name,)).fetchone()
    if not r:
        raise Forbidden(f"requesting user {name!r} is not active")
    return User(r["id"], r["name"], r["role"])


# ----------------------------------------------------------------- handlers: (conn, user, payload) -> dict


def _h_request_approvals(conn, user, p):
    """Escalate large proposed classifications for an approver's decision (idempotent per transaction)."""
    thr = int(setting(conn, "approval_threshold_minor"))
    n = 0
    for t in conn.execute("SELECT * FROM transactions WHERE classification_status='proposed' AND ABS(amount_minor)>=?", (thr,)).fetchall():
        aid = request_approval(conn, user, "classification_review", {"record_id": t["record_id"], "category": t["category"]},
                               abs(t["amount_minor"]), t["currency"], idem=f"classify:{t['record_id']}")
        n += 1
    return {"approval_requests": n}


HANDLERS = {
    "import_csv": lambda c, u, p: import_csv(c, u, p["account_id"], p["content"], p.get("filename", "upload.csv")),
    "import_stripe": lambda c, u, p: import_stripe_balance_transactions(c, u, p["account_id"], p["bts"], p.get("filename", "stripe.json"), p.get("now_epoch")),
    "detect_duplicates": lambda c, u, p: bookkeeping.detect_duplicates(c, u),
    "categorise": lambda c, u, p: bookkeeping.categorise(c, u),
    "match": lambda c, u, p: bookkeeping.match_transactions(c, u),
    "scan_flags": lambda c, u, p: bookkeeping.scan_flags(c, u),
    "request_approvals": _h_request_approvals,
    "propose_entries": lambda c, u, p: bookkeeping.propose_entries(c, u),
    "propose_invoice_entries": lambda c, u, p: bookkeeping.propose_invoice_entries(c, u),
    "reconcile": lambda c, u, p: bookkeeping.reconciliation_report(c, u, p["account_id"], p["period"]),
    "forecast_13w": lambda c, u, p: treasury.scenarios_13w(c, u, p["as_of"]),
    "revenue_report": lambda c, u, p: revenue.revenue_report(c, p["from"], p["to"]),
    "tax_calendar": lambda c, u, p: tax.build_calendar(c, u, p["as_of"]),
    "fpa_scenarios": lambda c, u, p: fpa.scenario_model(c, u, p.get("params")),
    "monthly_report": lambda c, u, p: reporting.monthly_report(c, u, p["period"]),
    "investor_pack": lambda c, u, p: reporting.investor_pack(c, u, p["period"], p.get("out_dir")),
}
AGENT_OF = {"import_csv": 1, "import_stripe": 1, "detect_duplicates": 1, "categorise": 1, "match": 1, "scan_flags": 1, "propose_entries": 1, "propose_invoice_entries": 1,
            "reconcile": 1, "request_approvals": 1, "forecast_13w": 2, "revenue_report": 3, "tax_calendar": 5, "fpa_scenarios": 6, "monthly_report": 7, "investor_pack": 7}


# ----------------------------------------------------------------- validation


def validate_output(x, path="result"):
    """Amounts must be integers (or null = unavailable), currencies valid ISO codes, dates parseable."""
    if isinstance(x, dict):
        if "currency" in x and x["currency"] is not None:
            try:
                check_currency(x["currency"])
            except MoneyError as e:
                raise ValidationFailure(f"{path}: {e}") from None
        if "minor" in x and x["minor"] is not None and (not isinstance(x["minor"], int) or isinstance(x["minor"], bool)):
            raise ValidationFailure(f"{path}.minor must be an integer, got {x['minor']!r}")
        for k, v in x.items():
            if k in ("as_of", "due_date", "week_start", "transaction_date", "entry_date") and isinstance(v, str):
                try:
                    date.fromisoformat(v[:10])
                except ValueError:
                    raise ValidationFailure(f"{path}.{k}: invalid date {v!r}") from None
            if k.endswith("_minor") and v is not None and (not isinstance(v, int) or isinstance(v, bool)):
                raise ValidationFailure(f"{path}.{k} must be an integer number of minor units, got {v!r}")
            validate_output(v, f"{path}.{k}")
    elif isinstance(x, list):
        for i, v in enumerate(x):
            validate_output(v, f"{path}[{i}]")


# ----------------------------------------------------------------- queue


def submit(conn, user: User, type_: str, payload: dict | None = None, idem: str | None = None, depends_on: list[int] | None = None, max_attempts=3) -> int:
    user.require("run_tasks")
    if type_ not in HANDLERS:
        raise ControlViolation(f"unknown task type {type_!r}")
    body = json.dumps(payload or {}, sort_keys=True, default=str)
    if idem is None:
        idem = f"{type_}:{hashlib.sha256(body.encode()).hexdigest()[:16]}:{now()}"
    with tx(conn):
        ex = conn.execute("SELECT id FROM tasks WHERE idempotency_key=?", (idem,)).fetchone()
        if ex:
            return ex[0]
        cur = conn.execute("INSERT INTO tasks (type,payload_json,idempotency_key,depends_on,max_attempts,requested_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                           (type_, body, idem, json.dumps(depends_on or []), max_attempts, user.name, now(), now()))
        audit(conn, user.name, "task.submit", "task", cur.lastrowid, None, {"type": type_, "agent": AGENT_OF.get(type_)})
        return cur.lastrowid


def _runnable(conn, t, at):
    deps = json.loads(t["depends_on"] or "[]")
    for d in deps:
        st = conn.execute("SELECT status FROM tasks WHERE id=?", (d,)).fetchone()[0]
        if st == "failed" or st == "blocked":
            return "blocked"
        if st != "done":
            return "wait"
    if t["next_run_at"] and t["next_run_at"] > at:
        return "wait"
    return "run"


def run_pending(conn, max_tasks=100, at: str | None = None) -> dict:
    at = at or _iso_ts()
    ran = failed = retried = blocked = 0
    progressed = True
    while progressed and ran + failed + retried < max_tasks:
        progressed = False
        for t in conn.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY id").fetchall():
            verdict = _runnable(conn, t, at)
            if verdict == "wait":
                continue
            progressed = True
            if verdict == "blocked":
                with tx(conn):
                    conn.execute("UPDATE tasks SET status='blocked', error='dependency failed', updated_at=? WHERE id=?", (now(), t["id"]))
                blocked += 1
                continue
            res = _execute(conn, t)
            ran += res == "done"
            failed += res == "failed"
            retried += res == "retry"
    return {"done": ran, "failed": failed, "retried": retried, "blocked": blocked}


def _execute(conn, t) -> str:
    conn.execute("UPDATE tasks SET status='running', attempts=attempts+1, updated_at=? WHERE id=?", (now(), t["id"]))
    attempts = t["attempts"] + 1
    try:
        user = _user_for(conn, t["requested_by"])
        out = HANDLERS[t["type"]](conn, user, json.loads(t["payload_json"]))
        validate_output(out)
        conn.execute("UPDATE tasks SET status='done', result_json=?, error=NULL, updated_at=? WHERE id=?", (json.dumps(out, default=str), now(), t["id"]))
        return "done"
    except TransientError as e:
        if attempts < t["max_attempts"]:
            conn.execute("UPDATE tasks SET status='queued', error=?, next_run_at=?, updated_at=? WHERE id=?", (f"transient: {e}", _iso_ts(30 * 2 ** attempts), now(), t["id"]))
            return "retry"
        _fail(conn, t, f"retries exhausted: {e}")
        return "failed"
    except Exception as e:  # permanent: control violation, forbidden, validation, bug
        _fail(conn, t, f"{type(e).__name__}: {e}")
        return "failed"


def _fail(conn, t, msg):
    with tx(conn):
        conn.execute("UPDATE tasks SET status='failed', error=?, updated_at=? WHERE id=?", (msg, now(), t["id"]))
        conn.execute("INSERT OR IGNORE INTO flags (entity_type,entity_id,code,severity,detail,created_at) VALUES ('task',?, 'task_failed','material',?,?)", (str(t["id"]), f"{t['type']}: {msg}"[:300], now()))
        audit(conn, "orchestrator", "task.failed", "task", t["id"], None, {"type": t["type"], "error": msg[:300]})


# ----------------------------------------------------------------- workflows


def submit_import_workflow(conn, user: User, source: str, account_id: int, data, period: str, filename: str = "upload") -> dict:
    """import -> duplicates -> categorise -> match -> flag -> request approvals -> propose entries -> reconciliation report."""
    user.require("run_tasks")
    raw = data if isinstance(data, str) else json.dumps(data, sort_keys=True)
    key = hashlib.sha256(f"{source}:{account_id}:{raw}".encode()).hexdigest()[:20]
    payload = {"account_id": account_id, "filename": filename}
    payload["content" if source == "csv" else "bts"] = data
    ids = {}
    ids["import"] = submit(conn, user, "import_csv" if source == "csv" else "import_stripe", payload, idem=f"wf:{key}:import")
    prev = [ids["import"]]
    for name, typ, pl in (("duplicates", "detect_duplicates", {}), ("categorise", "categorise", {}), ("match", "match", {}), ("flags", "scan_flags", {}),
                          ("approvals", "request_approvals", {}), ("entries", "propose_entries", {}), ("reconcile", "reconcile", {"account_id": account_id, "period": period})):
        ids[name] = submit(conn, user, typ, pl, idem=f"wf:{key}:{name}", depends_on=prev)
        prev = [ids[name]]
    return ids


# ----------------------------------------------------------------- consolidated view


def summary(conn, as_of: str | None = None) -> dict:
    base = setting(conn, "base_currency")
    cnt = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    ok, bad = verify_audit_chain(conn)
    cash = treasury.cash_position(conn, base)
    return {
        "base_currency": base, "cash": cash,
        "transactions": {"total": cnt("SELECT COUNT(*) FROM transactions"),
                         "uncategorised": cnt("SELECT COUNT(*) FROM transactions WHERE classification_status='uncategorised'"),
                         "proposed": cnt("SELECT COUNT(*) FROM transactions WHERE classification_status='proposed'"),
                         "approved": cnt("SELECT COUNT(*) FROM transactions WHERE classification_status='approved'"),
                         "unreconciled": cnt("SELECT COUNT(*) FROM transactions WHERE reconciliation_status IN ('unreconciled','exception')"),
                         "pending_settlement": cnt("SELECT COUNT(*) FROM transactions WHERE status='pending'")},
        "flags": {"open": cnt("SELECT COUNT(*) FROM flags WHERE status='open'"), "material": cnt("SELECT COUNT(*) FROM flags WHERE status='open' AND severity='material'")},
        "approvals_pending": cnt("SELECT COUNT(*) FROM approvals WHERE status='pending'"),
        "journal_drafts": cnt("SELECT COUNT(*) FROM journal_entries WHERE status='draft'"),
        "tasks": {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status")},
        "last_import": (conn.execute("SELECT filename,imported_at,rows_imported FROM import_batches ORDER BY id DESC LIMIT 1").fetchone() or None) and dict(conn.execute("SELECT filename,imported_at,rows_imported FROM import_batches ORDER BY id DESC LIMIT 1").fetchone()),
        "audit_chain_ok": ok, "audit_chain_broken_at": bad,
        "banner": reporting.BANNER,
    }
