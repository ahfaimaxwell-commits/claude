"""SQLite schema and accounting controls (enforced by triggers, not just application code)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

BASE_CURRENCY = "SGD"

SCHEMA = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL CHECK (role IN ('finance_admin','approver','preparer','viewer')),
  token_hash TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, institution TEXT,
  type TEXT NOT NULL CHECK (type IN ('bank','payment_processor','card','other')),
  currency TEXT NOT NULL CHECK (length(currency)=3),
  funds_class TEXT NOT NULL DEFAULT 'operating' CHECK (funds_class IN ('operating','restricted','customer_funds')),
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);

-- Cash is only ever reported from verified snapshots (statement / API read with evidence).
CREATE TABLE IF NOT EXISTS balance_snapshots (
  id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
  as_of TEXT NOT NULL, amount_minor INTEGER NOT NULL, currency TEXT NOT NULL,
  source TEXT NOT NULL, evidence_ref TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE (account_id, as_of, source));

CREATE TABLE IF NOT EXISTS import_batches (
  id INTEGER PRIMARY KEY, source_type TEXT NOT NULL, filename TEXT, account_id INTEGER,
  content_sha256 TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
  rows_total INTEGER, rows_imported INTEGER, rows_duplicate_source INTEGER, rows_rejected INTEGER,
  imported_by TEXT NOT NULL, imported_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS import_rejects (
  id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES import_batches(id),
  row_number INTEGER, reason TEXT NOT NULL, raw TEXT);

CREATE TABLE IF NOT EXISTS source_records (
  id INTEGER PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, source_type TEXT NOT NULL,
  batch_id INTEGER REFERENCES import_batches(id), raw_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL, imported_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS periods (id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  closed_by TEXT, closed_at TEXT);

CREATE TABLE IF NOT EXISTS coa (
  code TEXT PRIMARY KEY, name TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('asset','liability','equity','revenue','expense')),
  active INTEGER NOT NULL DEFAULT 1);

CREATE TABLE IF NOT EXISTS transactions (
  record_id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES source_records(id),
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  transaction_date TEXT NOT NULL, amount_minor INTEGER NOT NULL, currency TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('income','expense','transfer','refund','fee','other')),
  category TEXT REFERENCES coa(code), counterparty TEXT, description TEXT,
  status TEXT NOT NULL DEFAULT 'settled' CHECK (status IN ('pending','settled','failed')),
  accounting_period TEXT NOT NULL REFERENCES periods(id),
  classification_status TEXT NOT NULL DEFAULT 'uncategorised'
     CHECK (classification_status IN ('uncategorised','proposed','approved')),
  confidence REAL, approval_status TEXT NOT NULL DEFAULT 'not_required',
  reconciliation_status TEXT NOT NULL DEFAULT 'unreconciled'
     CHECK (reconciliation_status IN ('unreconciled','matched','exception','reconciled')),
  parent_record_id INTEGER REFERENCES transactions(record_id),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_txn_date ON transactions(transaction_date);
CREATE INDEX IF NOT EXISTS ix_txn_account ON transactions(account_id);

CREATE TABLE IF NOT EXISTS payment_events (
  event_id TEXT PRIMARY KEY, provider TEXT NOT NULL, event_type TEXT NOT NULL, livemode INTEGER,
  object_id TEXT, amount_minor INTEGER, currency TEXT, signature_verified INTEGER NOT NULL,
  payload_json TEXT NOT NULL, received_at TEXT NOT NULL,
  settlement_status TEXT NOT NULL DEFAULT 'unsettled' CHECK (settlement_status IN ('unsettled','settled','failed','not_applicable')));

CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY, direction TEXT NOT NULL CHECK (direction IN ('receivable','payable')),
  number TEXT, counterparty TEXT NOT NULL, issue_date TEXT NOT NULL, due_date TEXT,
  amount_minor INTEGER NOT NULL, tax_minor INTEGER NOT NULL DEFAULT 0, currency TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('draft','open','paid','void')),
  product TEXT, payment_terms TEXT, source_id INTEGER REFERENCES source_records(id),
  approval_status TEXT NOT NULL DEFAULT 'pending' CHECK (approval_status IN ('not_required','pending','approved','rejected')),
  accounting_period TEXT NOT NULL REFERENCES periods(id),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (direction, counterparty, number, currency));

CREATE TABLE IF NOT EXISTS receipts (
  id INTEGER PRIMARY KEY, vendor TEXT NOT NULL, receipt_date TEXT NOT NULL, amount_minor INTEGER NOT NULL,
  tax_minor INTEGER NOT NULL DEFAULT 0, currency TEXT NOT NULL, file_ref TEXT, source_id INTEGER REFERENCES source_records(id),
  created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS vendors (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, payment_terms TEXT, bank_details_ref TEXT,
  recurring INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS matches (
  id INTEGER PRIMARY KEY, transaction_id INTEGER NOT NULL REFERENCES transactions(record_id),
  doc_type TEXT NOT NULL CHECK (doc_type IN ('invoice','receipt','payment_event')), doc_id TEXT NOT NULL,
  score REAL NOT NULL, status TEXT NOT NULL CHECK (status IN ('proposed','confirmed','rejected')),
  reason TEXT, created_at TEXT NOT NULL, UNIQUE (transaction_id, doc_type, doc_id));

CREATE TABLE IF NOT EXISTS flags (
  id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, code TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('info','warning','material')), detail TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved','dismissed')),
  resolved_by TEXT, resolved_note TEXT, created_at TEXT NOT NULL,
  UNIQUE (entity_type, entity_id, code));

CREATE TABLE IF NOT EXISTS category_rules (
  id INTEGER PRIMARY KEY, pattern TEXT NOT NULL, coa_code TEXT NOT NULL REFERENCES coa(code),
  kind TEXT NOT NULL, confidence REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1);

CREATE TABLE IF NOT EXISTS journal_entries (
  id INTEGER PRIMARY KEY, entry_date TEXT NOT NULL, period TEXT NOT NULL REFERENCES periods(id), memo TEXT NOT NULL,
  currency TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','posted','reversed')),
  kind TEXT NOT NULL DEFAULT 'standard' CHECK (kind IN ('standard','adjustment','reversal')),
  source_ref TEXT, reverses_id INTEGER REFERENCES journal_entries(id), created_by TEXT NOT NULL,
  posted_by TEXT, posted_at TEXT, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS journal_lines (
  id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL REFERENCES journal_entries(id),
  account_code TEXT NOT NULL REFERENCES coa(code),
  debit_minor INTEGER NOT NULL DEFAULT 0 CHECK (debit_minor >= 0),
  credit_minor INTEGER NOT NULL DEFAULT 0 CHECK (credit_minor >= 0),
  currency TEXT NOT NULL,
  CHECK ((debit_minor = 0) <> (credit_minor = 0)));

CREATE TABLE IF NOT EXISTS budgets (
  id INTEGER PRIMARY KEY, period TEXT NOT NULL, department TEXT NOT NULL, category TEXT NOT NULL REFERENCES coa(code),
  amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0), currency TEXT NOT NULL, spend_limit_minor INTEGER,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE (period, department, category));

CREATE TABLE IF NOT EXISTS planned_items (
  id INTEGER PRIMARY KEY, description TEXT NOT NULL, item_date TEXT NOT NULL, amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('purchase','funding','revenue','hire','other')),
  basis TEXT NOT NULL DEFAULT 'assumption', created_by TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS metrics_inputs (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, period TEXT NOT NULL, value REAL NOT NULL,
  source TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0, UNIQUE (key, period));

CREATE TABLE IF NOT EXISTS forecasts (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, as_of TEXT NOT NULL,
  method TEXT NOT NULL, assumptions_json TEXT NOT NULL, sources_json TEXT NOT NULL, result_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS company_facts (
  key TEXT PRIMARY KEY, value TEXT, verified INTEGER NOT NULL DEFAULT 0, source TEXT, updated_by TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS tax_items (
  id INTEGER PRIMARY KEY, code TEXT NOT NULL, title TEXT NOT NULL, due_date TEXT, jurisdiction TEXT NOT NULL DEFAULT 'SG',
  basis TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','in_review','done','not_applicable')),
  professional_review TEXT NOT NULL DEFAULT 'required' CHECK (professional_review IN ('required','completed')),
  notes TEXT, UNIQUE (code, due_date));

CREATE TABLE IF NOT EXISTS open_questions (
  id INTEGER PRIMARY KEY, topic TEXT NOT NULL, question TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY, type TEXT NOT NULL, payload_json TEXT NOT NULL, amount_minor INTEGER, currency TEXT,
  requested_by TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','executed')),
  idempotency_key TEXT UNIQUE, decided_by TEXT, decided_at TEXT, decision_note TEXT, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS reconciliations (
  id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), period TEXT NOT NULL,
  opening_minor INTEGER, movements_minor INTEGER NOT NULL, expected_closing_minor INTEGER,
  statement_closing_minor INTEGER, difference_minor INTEGER, open_flags INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('complete','incomplete')), reasons_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS report_versions (
  id INTEGER PRIMARY KEY, report_type TEXT NOT NULL, period TEXT NOT NULL, version INTEGER NOT NULL,
  content_json TEXT NOT NULL, content_sha256 TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved')),
  created_by TEXT NOT NULL, approved_by TEXT, created_at TEXT NOT NULL, UNIQUE (report_type, period, version));

CREATE TABLE IF NOT EXISTS kpi_definitions (key TEXT PRIMARY KEY, name TEXT NOT NULL, definition TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS connectors (
  name TEXT PRIMARY KEY, status TEXT NOT NULL, supported_ops TEXT, permissions TEXT, sync_method TEXT, auth TEXT,
  error_handling TEXT, limitations TEXT, last_attempt_at TEXT, last_success_at TEXT, last_error TEXT);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY, type TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','done','failed','blocked')),
  idempotency_key TEXT UNIQUE, depends_on TEXT, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
  next_run_at TEXT, result_json TEXT, error TEXT, requested_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, entity TEXT, entity_id TEXT,
  before_json TEXT, after_json TEXT, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);

-- ---------- Controls ----------
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT,'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT,'audit_log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS source_no_update BEFORE UPDATE ON source_records BEGIN SELECT RAISE(ABORT,'source_records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS source_no_delete BEFORE DELETE ON source_records BEGIN SELECT RAISE(ABORT,'source_records are immutable'); END;

CREATE TRIGGER IF NOT EXISTS txn_immutable_core BEFORE UPDATE OF source_id, account_id, transaction_date, amount_minor, currency, kind ON transactions
BEGIN SELECT RAISE(ABORT,'transaction source fields are immutable; use a correcting entry'); END;
CREATE TRIGGER IF NOT EXISTS txn_no_delete BEFORE DELETE ON transactions BEGIN SELECT RAISE(ABORT,'transactions cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS txn_locked_class BEFORE UPDATE OF category ON transactions
WHEN OLD.classification_status='approved' AND NEW.category IS NOT OLD.category
BEGIN SELECT RAISE(ABORT,'approved classification is locked; use a reversal/correction'); END;
CREATE TRIGGER IF NOT EXISTS txn_closed_period BEFORE INSERT ON transactions
WHEN (SELECT status FROM periods WHERE id=NEW.accounting_period)='closed'
BEGIN SELECT RAISE(ABORT,'accounting period is closed'); END;

CREATE TRIGGER IF NOT EXISTS je_no_delete_posted BEFORE DELETE ON journal_entries WHEN OLD.status<>'draft'
BEGIN SELECT RAISE(ABORT,'posted journal entries cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS je_immutable BEFORE UPDATE ON journal_entries
WHEN OLD.status IN ('posted','reversed')
 AND NOT (OLD.status='posted' AND NEW.status='reversed' AND NEW.entry_date=OLD.entry_date AND NEW.period=OLD.period
          AND NEW.memo=OLD.memo AND NEW.currency=OLD.currency)
BEGIN SELECT RAISE(ABORT,'posted journal entries are immutable; post a reversal'); END;
CREATE TRIGGER IF NOT EXISTS je_post_checks BEFORE UPDATE OF status ON journal_entries
WHEN OLD.status='draft' AND NEW.status='posted'
BEGIN
  SELECT RAISE(ABORT,'period is not open') WHERE COALESCE((SELECT status FROM periods WHERE id=NEW.period),'')<>'open';
  SELECT RAISE(ABORT,'journal needs at least two lines') WHERE (SELECT COUNT(*) FROM journal_lines WHERE entry_id=NEW.id)<2;
  SELECT RAISE(ABORT,'journal is not balanced') WHERE
    (SELECT COALESCE(SUM(debit_minor),0) FROM journal_lines WHERE entry_id=NEW.id)
    <> (SELECT COALESCE(SUM(credit_minor),0) FROM journal_lines WHERE entry_id=NEW.id);
  SELECT RAISE(ABORT,'journal lines must all use the entry currency') WHERE
    (SELECT COUNT(*) FROM journal_lines WHERE entry_id=NEW.id AND currency<>NEW.currency)>0;
END;
CREATE TRIGGER IF NOT EXISTS jl_ins_locked BEFORE INSERT ON journal_lines
WHEN (SELECT status FROM journal_entries WHERE id=NEW.entry_id)<>'draft'
BEGIN SELECT RAISE(ABORT,'cannot add lines to a posted entry'); END;
CREATE TRIGGER IF NOT EXISTS jl_upd_locked BEFORE UPDATE ON journal_lines
WHEN (SELECT status FROM journal_entries WHERE id=OLD.entry_id)<>'draft'
BEGIN SELECT RAISE(ABORT,'posted journal lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS jl_del_locked BEFORE DELETE ON journal_lines
WHEN (SELECT status FROM journal_entries WHERE id=OLD.entry_id)<>'draft'
BEGIN SELECT RAISE(ABORT,'posted journal lines are immutable'); END;

CREATE TRIGGER IF NOT EXISTS report_approved_immutable BEFORE UPDATE ON report_versions WHEN OLD.status='approved'
BEGIN SELECT RAISE(ABORT,'approved report versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS period_no_reopen BEFORE UPDATE OF status ON periods WHEN OLD.status='closed' AND NEW.status='open'
BEGIN SELECT RAISE(ABORT,'closed periods cannot be reopened without a controlled procedure'); END;
"""

DEFAULT_SETTINGS = {
    "base_currency": BASE_CURRENCY,
    "enforce_separation_of_duties": "1",
    "approval_threshold_minor": "100000",       # >= SGD 1,000 needs an approver for classification/payments
    "material_adjustment_minor": "500000",       # >= SGD 5,000 adjustments are material
    "missing_receipt_threshold_minor": "2500",   # expenses >= SGD 25 need a receipt
    "unusual_spend_multiplier": "3.0",
    "auto_approve_classification": "0",          # policy: AI/rule proposals never self-approve
    "coa_approved_by_accountant": "0",
    "ar_collection_rate": "0.90",
    "ar_collection_delay_days": "7",
}

DEFAULT_COA = [
    ("1000", "Cash at bank - operating", "asset"), ("1010", "Payment processor clearing (unsettled)", "asset"),
    ("1050", "Restricted / customer-funds cash", "asset"), ("1100", "Accounts receivable", "asset"),
    ("1200", "Prepayments", "asset"), ("2000", "Accounts payable", "liability"),
    ("2100", "Customer funds held (not company money)", "liability"), ("2200", "GST payable", "liability"),
    ("2300", "Accrued expenses", "liability"), ("2400", "Unallocated receipts (suspense)", "liability"),
    ("3000", "Share capital", "equity"), ("3100", "Retained earnings", "equity"),
    ("4000", "Platform fee revenue", "revenue"), ("4100", "Visibility / promotion revenue", "revenue"),
    ("4090", "Refunds and credits (contra-revenue)", "revenue"), ("4900", "Other income", "revenue"),
    ("5000", "Payment processing fees", "expense"), ("5100", "Hosting and infrastructure", "expense"),
    ("6000", "Salaries and benefits", "expense"), ("6100", "Software subscriptions", "expense"),
    ("6200", "Marketing and advertising", "expense"), ("6300", "Professional fees", "expense"),
    ("6400", "Rent and office", "expense"), ("6500", "Travel", "expense"), ("6900", "Other operating expense", "expense"),
    ("7000", "Bank charges", "expense"), ("7100", "FX gain/loss", "expense"),
]

DEFAULT_RULES = [  # (pattern, coa, kind, confidence)
    ("stripe fee", "5000", "fee", 0.95), ("stripe processing", "5000", "fee", 0.95),
    ("supabase", "5100", "expense", 0.9), ("aws", "5100", "expense", 0.85), ("amazon web services", "5100", "expense", 0.9),
    ("lovable", "6100", "expense", 0.9), ("didit", "6100", "expense", 0.9), ("higgsfield", "6100", "expense", 0.9),
    ("anthropic", "6100", "expense", 0.9), ("google workspace", "6100", "expense", 0.9), ("github", "6100", "expense", 0.9),
    ("meta ads", "6200", "expense", 0.9), ("facebook ads", "6200", "expense", 0.9), ("google ads", "6200", "expense", 0.9),
    ("payroll", "6000", "expense", 0.9), ("salary", "6000", "expense", 0.9),
    ("acra", "6300", "expense", 0.8), ("iras", "6300", "expense", 0.5), ("accounting", "6300", "expense", 0.7),
    ("bank charge", "7000", "expense", 0.9),
]

KPI_DEFS = [
    ("gross_revenue", "Gross revenue", "Sum of approved receivable invoices issued in the period, excluding tax, before refunds. Cash receipts are not revenue."),
    ("net_revenue", "Net revenue", "Gross revenue less refunds/credits (account 4090) in the period. Processor fees are NOT deducted (they are cost of revenue)."),
    ("contribution_margin", "Contribution margin", "Net revenue less payment processing fees (5000) and hosting/infrastructure (5100), from posted journals."),
    ("net_burn", "Net burn", "Average monthly outflow less inflow from settled, approved operating transactions excluding transfers and customer funds, last 3 full months."),
    ("runway_months", "Runway (months)", "Verified operating cash divided by net burn. Estimate; unavailable without a verified cash balance."),
    ("cac", "Customer acquisition cost", "Marketing spend (6200) in period divided by new paying customers (metrics_inputs 'new_customers'). Unavailable without verified customer counts."),
]

CONNECTORS = [
    ("stripe", "not_verified",
     "Read balance transactions, charges, payouts; receive webhooks",
     "Read-only intended. No write/payment operations are implemented.",
     "Import of balance_transactions JSON; webhook events recorded as source events only",
     "Restricted API key or MCP connector, held server-side. Webhook signing secret from env STRIPE_WEBHOOK_SECRET.",
     "Idempotent on Stripe object id; failed batches roll back; events with bad signature are rejected.",
     "Events are NOT proof of settlement or revenue. Connect direct charges on landlord accounts are not in platform balance. Live access not yet verified.",
     None, None, None),
    ("bank_csv", "available",
     "Import statement CSV files", "Read-only (file upload)", "Manual file upload; sha256 de-duplication",
     "Role-gated upload (preparer+)", "Bad rows go to import_rejects; whole file rolls back on error",
     "No live bank feed. Balances are unavailable until a statement closing balance is recorded as a verified snapshot.",
     None, None, None),
    ("accounting_system", "not_connected", "None", "None", "None", "None", "None",
     "No Xero/QuickBooks connected. Chart of accounts is a draft pending accountant approval.", None, None, None),
    ("email_documents", "not_verified", "Receipts via Gmail/Outlook/SharePoint connectors (not yet wired)", "Read-only intended",
     "None yet; receipts currently entered through the API", "OAuth via claude.ai connectors", "n/a",
     "Not wired. Smallest next step: export receipts to a folder and ingest with the receipt parser.", None, None, None),
]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)  # explicit BEGIN/COMMIT
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    t = now()
    conn.execute("BEGIN")
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings VALUES (?,?,?)", (k, v, t))
    for code, name, typ in DEFAULT_COA:
        conn.execute("INSERT OR IGNORE INTO coa (code,name,type) VALUES (?,?,?)", (code, name, typ))
    if conn.execute("SELECT COUNT(*) FROM category_rules").fetchone()[0] == 0:
        conn.executemany("INSERT INTO category_rules (pattern,coa_code,kind,confidence) VALUES (?,?,?,?)", DEFAULT_RULES)
    for k, n, d in KPI_DEFS:
        conn.execute("INSERT OR IGNORE INTO kpi_definitions VALUES (?,?,?)", (k, n, d))
    for row in CONNECTORS:
        conn.execute("INSERT OR IGNORE INTO connectors (name,status,supported_ops,permissions,sync_method,auth,error_handling,limitations,last_attempt_at,last_success_at,last_error) VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
    for k in ("fiscal_year_end", "gst_registered", "incorporation_date", "entity_name"):
        conn.execute("INSERT OR IGNORE INTO company_facts (key,value,verified) VALUES (?,NULL,0)", (k,))
    conn.execute("COMMIT")


def setting(conn, key: str) -> str:
    return conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0]


def month_bounds(period: str) -> tuple[str, str]:
    """('2026-09-01', '2026-09-30') for '2026-09'."""
    from calendar import monthrange
    y, m = int(period[:4]), int(period[5:7])
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{monthrange(y, m)[1]:02d}"


def ensure_period(conn, date: str) -> str:
    p = date[:7]
    conn.execute("INSERT OR IGNORE INTO periods (id) VALUES (?)", (p,))
    return p
