# KeyFables Finance System

**Status:** initial build, exercised on synthetic data only. No live bank, payment or tax action is possible from this system.
All reports are *unaudited management information* and say so.

## 1. What exists

| Layer | Module | Purpose |
|---|---|---|
| Schema + controls | `kffinance/db.py` | SQLite schema; accounting controls are DB triggers, not just app code |
| Core | `core.py` | hash-chained audit log, RBAC, approvals, journal engine |
| Ingestion | `ingest.py` | CSV + Stripe balance-transaction import, webhook signature check, document field extraction |
| Agents | `agents/*.py` | the seven agents (below) |
| Orchestrator | `orchestrator.py` | task queue, dependencies, retries, output validation, escalation, consolidated summary |
| API + UI | `server.py`, `web/` | JSON API (server-side permissions) and the 13-view dashboard |

### Agents and their testable workflows
1. **Bookkeeping & reconciliation** (`bookkeeping.py`): categorise (proposals only), duplicate detection, invoice/receipt matching, missing-receipt/discrepancy/unusual-spend flags, proposed journals, reconciliation report that is `complete` **only** with verified opening and closing evidence, zero difference and nothing open.
2. **Treasury** (`treasury.py`): cash position from verified snapshots only (customer funds, restricted funds, unsettled items and foreign-currency accounts are never added to available cash); 13-week forecast with every line tagged `actual/scheduled/pending/estimate/assumption`; base/upside/downside; runway needs 3 months of data and verified cash.
3. **Revenue** (`revenue.py`): revenue from approved invoices only; cash receipts and customer funds reported separately as *not revenue*; webhook events de-duplicated by object and never treated as settlement; refunds, fees, contribution margin with written definitions; overdue and failed payments; pricing scenario with explicit elasticity assumption.
4. **Payables** (`payables.py`): invoice intake with duplicate detection, approval workflow, purchase requests vs budget, payment *authorisation* (never execution), bank-detail changes via approval, upcoming payables, recurring vendors.
5. **Tax & compliance** (`tax.py`): Singapore calendar (ECI, AGM, ACRA annual return, Form C-S/C) computed **only** from a verified year-end; GST status is never assumed; GST threshold monitor is labelled an estimate; deductible-expense evidence register; accountant handoff pack; `submit_tax_return` always refuses.
6. **FP&A** (`fpa.py`): budgets with variance on approved actuals; 12-month scenarios with sensitivity ranking; unit economics return "insufficient data" instead of guessing.
7. **Reporting** (`reporting.py`): P&L, balance sheet, cash-flow statement from *posted* journals; KPI definitions; monthly report with version history and approval (creator cannot approve); investor pack as a local file.

### Core workflow (one command in the orchestrator)
`import → duplicate scan → categorise → match → flag → request approvals → propose journals → reconciliation report`
(`orchestrator.submit_import_workflow`; the same workflow also runs from the dashboard CSV import).

## 2. Controls enforced in the database
- Transactions: source fields (`source_id, account, date, amount, currency, kind`) immutable; no deletes; approved classification locked; closed periods reject inserts.
- `source_records`: immutable, unique `source_key` = idempotency for every imported record.
- Journals: post only if balanced, ≥2 lines, one currency, period open; posted entries/lines immutable; correction = reversal entry; periods cannot be reopened.
- `audit_log`: append-only triggers **and** a SHA-256 hash chain (`verify-audit`) so tampering by someone with raw DB access is detectable.
- Approved report versions are immutable.

## 3. Security model
- Roles: `viewer`, `preparer`, `approver`, `finance_admin`. Enforced in server functions; the browser only mirrors it.
- Separation of duties (configurable, default on): requesters cannot decide their own approvals; creators cannot post their own journals or approve their own reports.
- Always need an approval: payments, vendor bank-detail changes, tax submissions (also needs recorded professional review), permission changes (needs a `finance_admin`), expenditure, external exports, period close, material adjustments (≥ SGD 5,000).
- AI/rule automation runs as `system` with `preparer` rights: it can propose and flag, never approve or post.
- Tokens are random, stored as SHA-256 hashes, shown once. API binds to 127.0.0.1; strict CSP (no inline scripts); the UI never uses `innerHTML`; request bodies capped at 8 MB; logs never contain bodies or tokens; internal errors return no stack traces.
- Secrets: only `STRIPE_WEBHOOK_SECRET` (env var) is read, server-side. No keys in code. Without it the webhook endpoint refuses events.
- Put the server behind TLS (e.g. a reverse proxy) before exposing it beyond localhost.

## 4. Connectors (truth table)
| Connector | State | Notes |
|---|---|---|
| Bank CSV | works | upload only; no live feed. Balances stay *Unavailable* until a statement closing balance is recorded and verified by an approver |
| Stripe | importer + webhook written and tested on sample payloads; **live access not verified** | read-only by design; Connect direct charges on landlord accounts are not in the platform balance |
| Accounting system (Xero/QuickBooks) | not connected | none exists today; smallest next step is a CSV export of journals for the accountant |
| Gmail / Outlook / SharePoint receipts | not wired | the receipt parser exists; smallest step is exporting receipts to a folder and ingesting |
Connector `status` becomes `verified` only after a real import succeeds.

## 5. Operations
- **Backup:** `python3 -m kffinance backup --out <file>` (SQLite online backup, consistent). Schedule daily; keep copies off the machine.
- **Restore:** stop the server, copy the backup over `data/kffinance.db`, run `python3 -m kffinance verify-audit`.
- **First use:** `init` → set company facts (year-end, GST status, with sources) → record verified opening balances → import statements.
- Synthetic demo: `seed-synthetic` refuses to touch the default database.
- Cost: £0/US$0: no services, no dependencies. Optional later costs: hosting, a bank feed, an accounting system.

## 6. Known limitations / open items
- **Not a substitute** for a qualified accountant or tax adviser. The default chart of accounts is a *draft* (`coa_approved_by_accountant=0`) and is flagged as such.
- Tax calendar rules are the system's understanding of IRAS/ACRA requirements and are labelled "confirm with your accountant"; GST return dates need the IRAS-assigned stagger and are intentionally not computed.
- Company funds vs customer funds treatment for the Stripe Connect landlord payments **awaits counsel**; until then any account marked `customer_funds` is excluded from cash, revenue and runway, and journals credit liability `2100`.
- Revenue is recognised from approved invoices (accrual). Whether platform-fee invoices should be issued per lease month, and the rent/deposit/fee fee model, are open decisions outside this system.
- The existing app's `invoices.gst` column exists but GST registration is unverified: invoices carrying tax are blocked from journalling until that is resolved.
- FX: only same-currency arithmetic; other-currency items are excluded and listed. Add rates (with source and date) before relying on multi-currency totals.
- Forecast quality depends on current imports; items older than the forecast start are assumed paid.
- Single-node SQLite; concurrent writes serialise. Adequate for this scale; migrate before multi-user growth.
- No email/SSO login; tokens only. Token rotation is manual (deactivate via a permission-change approval).
- Live Stripe/bank data has not been imported. Everything above was verified on synthetic or sample data.

## 7. Verification
`cd tests && python3 -m unittest discover -p "test_*.py"` (88 tests: money, journal controls, audit tamper detection, SoD, RBAC over HTTP, duplicate-import prevention, webhook signatures, reconciliation gating, forecasts, tax, FP&A, reporting, backup/restore, CLI guards). The dashboard was also rendered in headless Chromium: all 13 views loaded with no JS errors and no mobile overflow.
