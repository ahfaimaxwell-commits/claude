"""HTTP API + dashboard host (stdlib only). All permission checks happen here and in the core, never in the browser."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote_plus

from . import db as dbm
from . import orchestrator as orch
from .agents import bookkeeping, fpa, payables, reporting, revenue, tax, treasury
from .core import (SYSTEM, ControlViolation, Forbidden, User, audit, authenticate, create_entry, decide_approval, finalize_reversal, post_entry,
                   request_approval, reverse_entry, trial_balance, tx, verify_audit_chain)
from .db import month_bounds, now, setting
from .money import MoneyError, obj, parse_amount

WEB = Path(__file__).parent / "web"
MAX_BODY = 8 * 1024 * 1024
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'"


def rows(conn, sql, *a):
    return [dict(r) for r in conn.execute(sql, a)]


def _m(r, key="amount_minor", ccy="currency"):
    return obj(r[key], r[ccy])


# ------------------------------------------------------------------ GET handlers: (conn, user, query) -> json


def g_overview(c, u, q):
    s = orch.summary(c)
    base = s["base_currency"]
    today = date.today().isoformat()
    first = today[:7] + "-01"
    s["revenue_this_month"] = revenue.revenue_report(c, first, today, base)
    s["expenses_this_month"] = obj(-(c.execute("SELECT COALESCE(SUM(amount_minor),0) FROM transactions WHERE kind IN ('expense','fee') AND status='settled' AND currency=? AND transaction_date>=?", (base, first)).fetchone()[0]), base)
    s["upcoming_obligations"] = payables.upcoming_payables(c, today, 30, base)
    s["dataset_label"] = setting_or(c, "dataset_label")
    s["user"] = {"name": u.name, "role": u.role}
    return s


def setting_or(c, k):
    r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    return r[0] if r else None


def g_transactions(c, u, q):
    where, args = ["1=1"], []
    if q.get("q"):
        where.append("(lower(t.description) LIKE ? OR lower(COALESCE(t.counterparty,'')) LIKE ?)")
        args += [f"%{q['q'].lower()}%"] * 2
    for k, col in (("classification", "classification_status"), ("reconciliation", "reconciliation_status"), ("status", "status"), ("kind", "kind"), ("period", "accounting_period")):
        if q.get(k):
            where.append(f"t.{col}=?"); args.append(q[k])
    limit = min(int(q.get("limit", 100)), 500)
    off = int(q.get("offset", 0))
    total = c.execute(f"SELECT COUNT(*) FROM transactions t WHERE {' AND '.join(where)}", args).fetchone()[0]
    data = c.execute(f"""SELECT t.*, s.source_key, a.name account_name, a.funds_class FROM transactions t JOIN source_records s ON s.id=t.source_id
                         JOIN accounts a ON a.id=t.account_id WHERE {' AND '.join(where)} ORDER BY t.transaction_date DESC, t.record_id DESC LIMIT ? OFFSET ?""", args + [limit, off]).fetchall()
    return {"total": total, "items": [{**{k: r[k] for k in ("record_id", "source_id", "source_key", "transaction_date", "currency", "kind", "category", "counterparty", "description", "status",
            "accounting_period", "classification_status", "confidence", "approval_status", "reconciliation_status", "account_name", "funds_class", "created_at", "updated_at")},
            "amount": _m(r), "open_flags": c.execute("SELECT COUNT(*) FROM flags WHERE entity_type='transaction' AND entity_id=? AND status='open'", (str(r["record_id"]),)).fetchone()[0]} for r in data]}


def g_revenue(c, u, q):
    today = date.today()
    return revenue.revenue_report(c, q.get("from", f"{today.year}-01-01"), q.get("to", today.isoformat()))


def g_expenses(c, u, q):
    today = date.today().isoformat()
    return {"payable_invoices": [{**r, "amount": obj(r["amount_minor"] + r["tax_minor"], r["currency"])} for r in rows(c, "SELECT * FROM invoices WHERE direction='payable' ORDER BY issue_date DESC LIMIT 200")],
            "receipts": [{**r, "amount": obj(r["amount_minor"], r["currency"])} for r in rows(c, "SELECT * FROM receipts ORDER BY receipt_date DESC LIMIT 200")],
            "vendors": rows(c, "SELECT id,name,payment_terms,recurring FROM vendors"), "upcoming": payables.upcoming_payables(c, today),
            "recurring": payables.recurring_vendors(c), "savings_observations": payables.savings_opportunities(c),
            "pending_purchase_requests": rows(c, "SELECT id,payload_json,amount_minor,currency,requested_by,created_at FROM approvals WHERE type='expenditure' AND status='pending'")}


def g_cashflow(c, u, q):
    as_of = q.get("as_of", date.today().isoformat())
    return {"cash": treasury.cash_position(c), "runway": treasury.runway(c, date.fromisoformat(as_of)), "forecasts": treasury.scenarios_13w(c, SYSTEM, as_of, save=False),
            "planned_items": rows(c, "SELECT * FROM planned_items ORDER BY item_date")}


def g_budgets(c, u, q):
    period = q.get("period", date.today().isoformat()[:7])
    return fpa.budget_vs_actual(c, period)


def g_accounting(c, u, q):
    return {"coa": rows(c, "SELECT * FROM coa ORDER BY code"), "coa_approved_by_accountant": setting(c, "coa_approved_by_accountant") == "1",
            "entries": [{**r, "total": obj(r["total"], r["currency"])} for r in rows(c, """SELECT e.*, (SELECT SUM(debit_minor) FROM journal_lines WHERE entry_id=e.id) total FROM journal_entries e ORDER BY e.id DESC LIMIT 200""")],
            "periods": rows(c, "SELECT * FROM periods ORDER BY id DESC"), "reconciliations": rows(c, "SELECT * FROM reconciliations ORDER BY id DESC LIMIT 50"),
            "trial_balance": trial_balance(c), "accounts": rows(c, "SELECT * FROM accounts")}


def g_journal(c, u, q):
    eid = int(q["id"])
    return {"entry": rows(c, "SELECT * FROM journal_entries WHERE id=?", eid), "lines": rows(c, "SELECT * FROM journal_lines WHERE entry_id=?", eid)}


def g_tax(c, u, q):
    today = date.today().isoformat()
    return {"calendar": rows(c, "SELECT * FROM tax_items ORDER BY due_date"), "facts": rows(c, "SELECT * FROM company_facts"), "open_questions": rows(c, "SELECT * FROM open_questions WHERE status='open'"),
            "gst_threshold": tax.gst_threshold_monitor(c, today), "missing_documentation": tax.missing_documentation(c), "deductibles": tax.deductible_register(c),
            "disclaimer": "Potentially applicable obligations only. Not tax advice; requires professional review. Filing is never automated."}


def g_planning(c, u, q):
    return {"scenarios": fpa.scenario_model(c, SYSTEM, None, save=False), "unit_economics": fpa.unit_economics(c, q.get("period", date.today().isoformat()[:7])),
            "forecasts_saved": rows(c, "SELECT id,name,kind,as_of,created_by,created_at FROM forecasts ORDER BY id DESC LIMIT 20")}


def g_investor(c, u, q):
    p = q.get("period", date.today().isoformat()[:7])
    return {"kpis": reporting.kpis(c, *month_bounds(p)), "versions": rows(c, "SELECT id,report_type,period,version,status,created_by,approved_by,created_at,content_sha256 FROM report_versions ORDER BY id DESC LIMIT 50"),
            "banner": reporting.BANNER}


def g_report(c, u, q):
    r = c.execute("SELECT * FROM report_versions WHERE report_type=? AND period=? AND version=?", (q["type"], q["period"], int(q["version"]))).fetchone()
    return {"meta": {k: r[k] for k in ("report_type", "period", "version", "status", "created_by", "approved_by")}, "content": json.loads(r["content_json"])} if r else {}


def g_connectors(c, u, q):
    stripe_secret = bool(os.environ.get("STRIPE_WEBHOOK_SECRET"))
    return {"connectors": rows(c, "SELECT * FROM connectors"), "webhook_secret_configured": stripe_secret,
            "note": "Status 'verified' means a real import succeeded; it does not mean live bank/payment access. 'not_verified' means never successfully exercised."}


def g_approvals(c, u, q):
    st = q.get("status", "pending")
    return {"items": [{**r, "payload": json.loads(r.pop("payload_json")), "amount": obj(r["amount_minor"], r["currency"]) if r["amount_minor"] is not None else None}
                      for r in rows(c, "SELECT * FROM approvals WHERE status=? ORDER BY id DESC LIMIT 200", st)],
            "pending_matches": rows(c, "SELECT * FROM matches WHERE status='proposed' ORDER BY id DESC LIMIT 100")}


def g_flags(c, u, q):
    return {"items": rows(c, "SELECT * FROM flags WHERE status=? ORDER BY CASE severity WHEN 'material' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, id DESC LIMIT 300", q.get("status", "open"))}


def g_audit(c, u, q):
    ok, bad = verify_audit_chain(c)
    return {"chain_ok": ok, "broken_at": bad, "items": rows(c, "SELECT id,ts,actor,action,entity,entity_id,after_json FROM audit_log ORDER BY id DESC LIMIT ?", min(int(q.get("limit", 200)), 1000)),
            "imports": rows(c, "SELECT * FROM import_batches ORDER BY id DESC LIMIT 50"), "rejects": rows(c, "SELECT * FROM import_rejects ORDER BY id DESC LIMIT 100"),
            "tasks": rows(c, "SELECT id,type,status,attempts,error,requested_by,created_at FROM tasks ORDER BY id DESC LIMIT 100")}


GET = {"overview": g_overview, "transactions": g_transactions, "revenue": g_revenue, "expenses": g_expenses, "cashflow": g_cashflow, "budgets": g_budgets, "accounting": g_accounting,
       "journal": g_journal, "tax": g_tax, "planning": g_planning, "investor": g_investor, "report": g_report, "connectors": g_connectors, "approvals": g_approvals, "flags": g_flags, "audit": g_audit}


# ------------------------------------------------------------------ POST handlers: (conn, user, body, args) -> json


def _run(c, u, ids):
    res = orch.run_pending(c)
    tasks = rows(c, f"SELECT id,type,status,error,result_json FROM tasks WHERE id IN ({','.join('?' * len(ids))})", *ids)
    return {"run": res, "tasks": [{**t, "result": json.loads(t.pop("result_json")) if t.get("result_json") else None} for t in tasks]}


def p_import_csv(c, u, b, a):
    ids = orch.submit_import_workflow(c, u, "csv", int(b["account_id"]), b["content"], b.get("period") or date.today().isoformat()[:7], b.get("filename", "upload.csv"))
    return {"workflow": ids, **_run(c, u, list(ids.values()))}


def p_import_stripe(c, u, b, a):
    ids = orch.submit_import_workflow(c, u, "stripe", int(b["account_id"]), b["transactions"], b.get("period") or date.today().isoformat()[:7], b.get("filename", "stripe.json"))
    return {"workflow": ids, **_run(c, u, list(ids.values()))}


def p_run(c, u, b, a):
    tid = orch.submit(c, u, b["type"], b.get("payload"), b.get("idempotency_key"))
    return _run(c, u, [tid])


def p_decide(c, u, b, a):
    return decide_approval(c, u, int(a[0]), bool(b["approve"]), b.get("note", ""))


def p_classify(c, u, b, a):
    bookkeeping.classify_transaction(c, u, int(a[0]), b["category"], b.get("note", "")); return {"ok": True}


def p_confirm_match(c, u, b, a):
    bookkeeping.confirm_match(c, u, int(a[0]), bool(b["accept"])); return {"ok": True}


def p_resolve_flag(c, u, b, a):
    bookkeeping.resolve_flag(c, u, int(a[0]), b.get("note", ""), bool(b.get("dismiss"))); return {"ok": True}


def p_post_journal(c, u, b, a):
    post_entry(c, u, int(a[0]), b.get("approval_id")); return {"ok": True}


def p_reverse(c, u, b, a):
    return {"reversal_id": reverse_entry(c, u, int(a[0]), b["date"], b["reason"])}


def p_reversal_post(c, u, b, a):
    finalize_reversal(c, u, int(a[0])); return {"ok": True}


def p_draft_journal(c, u, b, a):
    lines = [(l["account"], parse_amount(l.get("debit", "0") or "0", b["currency"]), parse_amount(l.get("credit", "0") or "0", b["currency"])) for l in b["lines"]]
    kind = b.get("kind", "standard")
    return {"entry_id": create_entry(c, u, b["date"], b["memo"], lines, b["currency"], kind=kind)}


def p_close_request(c, u, b, a):
    u.require("close_period")
    p = a[0]
    blockers = []
    if c.execute("SELECT COUNT(*) FROM flags f JOIN transactions t ON f.entity_type='transaction' AND f.entity_id=CAST(t.record_id AS TEXT) WHERE f.status='open' AND f.severity='material' AND t.accounting_period=?", (p,)).fetchone()[0]:
        blockers.append("open material flags")
    if c.execute("SELECT COUNT(*) FROM journal_entries WHERE period=? AND status='draft'", (p,)).fetchone()[0]:
        blockers.append("draft journal entries")
    if blockers:
        raise ControlViolation(f"cannot request period close: {', '.join(blockers)}")
    return {"approval_id": request_approval(c, u, "period_close", {"period": p}, idem=f"close:{p}")}


def p_budget(c, u, b, a):
    ccy = b.get("currency", "SGD")
    fpa.set_budget(c, u, b["period"], b["department"], b["category"], parse_amount(b["amount"], ccy), ccy, parse_amount(b["limit"], ccy) if b.get("limit") else None); return {"ok": True}


def p_invoice(c, u, b, a):
    ccy = b["currency"]
    return payables.submit_invoice(c, u, b["direction"], b["counterparty"], b["number"], b["issue_date"], b.get("due_date"), parse_amount(b["amount"], ccy), parse_amount(b.get("tax", "0"), ccy), ccy, b.get("product"), b.get("payment_terms"))


def p_pay_auth(c, u, b, a):
    u.require("request_approval")
    return payables.authorise_payment(c, u, int(b["invoice_id"]))


def p_purchase(c, u, b, a):
    ccy = b.get("currency", "SGD")
    return payables.purchase_request(c, u, b["description"], parse_amount(b["amount"], ccy), ccy, b["category"], b["period"], b.get("department", "general"))


def p_snapshot(c, u, b, a):
    u.require("import")
    ccy = b["currency"]
    if not b.get("evidence_ref") or not b.get("source"):
        raise ControlViolation("a balance snapshot needs a source and an evidence reference (statement or API read)")
    verified = u.can("approve")  # preparers can submit; only approvers can verify
    with tx(c):
        c.execute("INSERT INTO balance_snapshots (account_id,as_of,amount_minor,currency,source,evidence_ref,verified,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (int(b["account_id"]), b["as_of"], parse_amount(b["amount"], ccy), ccy, b["source"], b["evidence_ref"], int(verified), u.name, now()))
        audit(c, u.name, "snapshot.record", "account", b["account_id"], None, {"as_of": b["as_of"], "verified": verified, "source": b["source"]})
    return {"verified": verified}


def p_account(c, u, b, a):
    u.require("manage_users")
    with tx(c):
        c.execute("INSERT INTO accounts (name,institution,type,currency,funds_class,created_at) VALUES (?,?,?,?,?,?)", (b["name"], b.get("institution"), b["type"], b["currency"], b.get("funds_class", "operating"), now()))
        audit(c, u.name, "account.create", "account", b["name"], None, {k: b.get(k) for k in ("type", "currency", "funds_class")})
    return {"ok": True}


def p_user(c, u, b, a):
    u.require("manage_users")  # permission changes always need a second person's approval
    return {"approval_id": request_approval(c, u, "permission_change", {"action": b["action"], "name": b["name"], "role": b.get("role")})}


def p_fact(c, u, b, a):
    tax.set_fact(c, u, b["key"], b["value"], bool(b.get("verified")), b.get("source", "")); return {"ok": True}


def p_planned(c, u, b, a):
    u.require("plan")
    ccy = b["currency"]
    with tx(c):
        c.execute("INSERT INTO planned_items (description,item_date,amount_minor,currency,kind,created_by,created_at) VALUES (?,?,?,?,?,?,?)", (b["description"], b["date"], parse_amount(b["amount"], ccy), ccy, b["kind"], u.name, now()))
        audit(c, u.name, "planned_item.add", "planned_item", b["description"], None, {"amount": b["amount"], "kind": b["kind"]})
    return {"ok": True}


def p_report(c, u, b, a):
    r = reporting.monthly_report(c, u, b["period"])
    return {"version": r["version"], "status": r["status"], "banner": r["banner"]}


def p_report_approve(c, u, b, a):
    reporting.approve_report(c, u, b["type"], b["period"], int(b["version"])); return {"ok": True}


def p_export_request(c, u, b, a):
    return {"approval_id": request_approval(c, u, "export_external", {"what": b["what"], "destination": b["destination"]})}


def p_tax_build(c, u, b, a):
    return tax.build_calendar(c, u, b.get("as_of", date.today().isoformat()))


def p_metric(c, u, b, a):
    u.require("approve")
    with tx(c):
        c.execute("INSERT OR REPLACE INTO metrics_inputs (key,period,value,source,verified) VALUES (?,?,?,?,1)", (b["key"], b["period"], float(b["value"]), b["source"]))
        audit(c, u.name, "metric.set", "metric", f"{b['key']}/{b['period']}", None, {"value": b["value"], "source": b["source"]})
    return {"ok": True}


POST = [
    (r"^/api/import/csv$", p_import_csv), (r"^/api/import/stripe$", p_import_stripe), (r"^/api/run$", p_run),
    (r"^/api/approvals/(\d+)/decide$", p_decide), (r"^/api/transactions/(\d+)/classify$", p_classify), (r"^/api/matches/(\d+)/confirm$", p_confirm_match),
    (r"^/api/flags/(\d+)/resolve$", p_resolve_flag), (r"^/api/journal/(\d+)/post$", p_post_journal), (r"^/api/journal/(\d+)/reverse$", p_reverse),
    (r"^/api/journal/(\d+)/reversal-post$", p_reversal_post), (r"^/api/journal$", p_draft_journal), (r"^/api/periods/([\d-]+)/close-request$", p_close_request),
    (r"^/api/budgets$", p_budget), (r"^/api/invoices$", p_invoice), (r"^/api/payments/authorise$", p_pay_auth), (r"^/api/purchase-requests$", p_purchase),
    (r"^/api/snapshots$", p_snapshot), (r"^/api/accounts$", p_account), (r"^/api/users$", p_user), (r"^/api/facts$", p_fact), (r"^/api/planned-items$", p_planned),
    (r"^/api/reports/monthly$", p_report), (r"^/api/reports/approve$", p_report_approve), (r"^/api/export-requests$", p_export_request), (r"^/api/tax/build$", p_tax_build),
    (r"^/api/metrics$", p_metric),
]


# ------------------------------------------------------------------ HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "kffinance"
    db_path = "data/kffinance.db"

    def log_message(self, fmt, *args):  # never log bodies, headers or tokens
        sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(data)

    def _conn(self):
        return dbm.connect(self.db_path)

    def _user(self, c):
        h = self.headers.get("Authorization", "")
        return authenticate(c, h[7:] if h.startswith("Bearer ") else None)

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path in ("/", "/index.html", "/app.js", "/style.css"):
            f = WEB / ("index.html" if path in ("/", "/index.html") else path[1:])
            return self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "text/plain")
        if path == "/favicon.ico":
            self.send_response(204); self.end_headers(); return
        if path == "/health":
            return self._send(200, {"ok": True})
        m = re.match(r"^/api/(\w+)$", path)
        if not m or m.group(1) not in GET:
            return self._send(404, {"error": "not found"})
        q = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        q = {k: unquote_plus(v) for k, v in q.items()}
        c = self._conn()
        try:
            u = self._user(c)
            if not u:
                return self._send(401, {"error": "authentication required"})
            u.require("read")
            return self._send(200, GET[m.group(1)](c, u, q))
        except Forbidden as e:
            return self._send(403, {"error": str(e)})
        except (ControlViolation, MoneyError, ValueError, KeyError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            sys.stderr.write(f"ERROR {type(e).__name__}: {e}\n")
            return self._send(500, {"error": "internal error"})
        finally:
            c.close()

    def do_POST(self):
        path = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._send(413, {"error": "payload too large"})
        raw = self.rfile.read(n)
        if path == "/webhooks/stripe":
            return self._webhook(raw)
        c = self._conn()
        try:
            u = self._user(c)
            if not u:
                return self._send(401, {"error": "authentication required"})
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid JSON"})
            for pat, fn in POST:
                m = re.match(pat, path)
                if m:
                    return self._send(200, fn(c, u, body, m.groups()))
            return self._send(404, {"error": "not found"})
        except Forbidden as e:
            return self._send(403, {"error": str(e)})
        except ControlViolation as e:
            return self._send(409, {"error": str(e)})
        except (MoneyError, ValueError, KeyError, TypeError) as e:
            return self._send(400, {"error": f"{type(e).__name__}: {e}"})
        except Exception as e:
            sys.stderr.write(f"ERROR {type(e).__name__}: {e}\n")
            return self._send(500, {"error": "internal error"})
        finally:
            c.close()

    def _webhook(self, raw):
        from .ingest import record_payment_event, verify_stripe_signature
        secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
        if not secret:
            return self._send(503, {"error": "webhook secret not configured; events are rejected"})
        if not verify_stripe_signature(raw, self.headers.get("Stripe-Signature", ""), secret):
            return self._send(400, {"error": "invalid signature"})
        c = self._conn()
        try:
            return self._send(200, record_payment_event(c, json.loads(raw), True))
        except Exception as e:
            sys.stderr.write(f"ERROR webhook {type(e).__name__}\n")
            return self._send(500, {"error": "processing failed; Stripe will retry"})  # 5xx => provider retries; recording is idempotent
        finally:
            c.close()


def serve(db_path: str, host="127.0.0.1", port=8800):
    Handler.db_path = db_path
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"KeyFables Finance Command Centre on http://{host}:{port}  (db: {db_path})")
    srv.serve_forever()
