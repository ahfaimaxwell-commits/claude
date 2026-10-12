"use strict";
// Data is rendered only through DOM text nodes and all permission checks are enforced by the server.
const SECTIONS = ["Overview", "Transactions", "Revenue", "Expenses", "Cash Flow", "Budgets", "Accounting", "Tax & Compliance", "Financial Planning", "Investor Reporting", "Connectors", "Approvals", "Audit Centre"];
let token = null, ME = null, current = "Overview";
try { token = sessionStorage.getItem("kff_token"); } catch (e) { token = null; }

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") el.className = v; else if (k.startsWith("on")) el.addEventListener(k.slice(2), v); else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}
const $ = (id) => document.getElementById(id);
function toast(msg) { const t = $("toast"); t.textContent = msg; t.classList.add("show"); setTimeout(() => t.classList.remove("show"), 3500); }

async function api(path, body) {
  const res = await fetch(path, { method: body ? "POST" : "GET", headers: { "Authorization": "Bearer " + token, "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
async function act(fn, ok) { try { const r = await fn(); toast(ok || "Done"); render(current); return r; } catch (e) { toast("Blocked: " + e.message); } }

// money object {available, display, minor}: unavailable is shown distinctly from zero
function money(m) { if (!m) return h("span", { class: "unav" }, "Unavailable"); return m.available === false ? h("span", { class: "unav", title: "No verified source" }, "Unavailable") : h("span", {}, m.display); }
function pill(text, kind) { return h("span", { class: "pill " + (kind || "") }, text); }
function stat(label, valueNode, note) { return h("div", { class: "card" }, h("div", { class: "l" }, label), h("div", { class: "v" }, valueNode), note ? h("div", { class: "n" }, note) : null); }
function table(cols, rows) {
  return h("div", { class: "tbl" }, h("table", {}, h("thead", {}, h("tr", {}, cols.map((c) => h("th", { class: c.num ? "num" : "" }, c.t)))),
    h("tbody", {}, rows.length ? rows.map((r) => h("tr", {}, cols.map((c) => { const v = c.f(r); return h("td", { class: (c.num ? "num " : "") + (c.wrap ? "wrap" : "") }, v); }))) : [h("tr", {}, h("td", { colspan: cols.length, class: "muted" }, "No records"))])));
}
const sevPill = (s) => pill(s, s === "material" ? "bad" : s === "warning" ? "warn" : "");
const statusPill = (s) => pill(s, ["approved", "complete", "done", "matched", "reconciled", "verified", "settled"].includes(s) ? "ok" : ["rejected", "failed", "exception", "incomplete", "closed"].includes(s) ? "bad" : ["pending", "proposed", "unreconciled", "not_verified", "draft"].includes(s) ? "warn" : "");

function section(title, ...nodes) { return h("section", {}, h("h2", {}, title), ...nodes); }
function listNote(items) { return h("ul", {}, items.map((i) => h("li", {}, i))); }

const VIEWS = {
  async "Overview"() {
    const o = await api("/api/overview");
    $("dataset").hidden = !o.dataset_label; $("dataset").textContent = o.dataset_label || "";
    const t = o.transactions;
    return [h("h1", {}, "Overview"),
      h("div", { class: "grid" },
        stat("Verified operating cash", money(o.cash.available_operating_cash), o.cash.note),
        stat("Revenue this month (invoiced)", money(o.revenue_this_month.net_revenue), o.revenue_this_month.data_available ? "Net of refunds" : "No approved invoices yet"),
        stat("Expenses this month (settled)", money(o.expenses_this_month), "Cash basis, settled only"),
        stat("Due in next 30 days", money(o.upcoming_obligations.total_base_currency), o.upcoming_obligations.items.length + " approved payable(s)"),
        stat("Outstanding approvals", h("span", {}, String(o.approvals_pending)), o.journal_drafts + " journal draft(s)"),
        stat("Open flags", h("span", {}, String(o.flags.open)), o.flags.material + " material"),
        stat("Audit chain", pill(o.audit_chain_ok ? "intact" : "BROKEN at " + o.audit_chain_broken_at, o.audit_chain_ok ? "ok" : "bad"))),
      section("Transactions", h("div", { class: "grid" }, stat("Total", String(t.total)), stat("Uncategorised", String(t.uncategorised)), stat("Proposed (awaiting review)", String(t.proposed)),
        stat("Approved", String(t.approved)), stat("Unreconciled", String(t.unreconciled)), stat("Pending settlement", String(t.pending_settlement)))),
      section("Accounts", table([{ t: "Account", f: (a) => a.name }, { t: "Class", f: (a) => a.funds_class }, { t: "Verified balance", num: 1, f: (a) => money(a.balance) }, { t: "As of", f: (a) => a.as_of || "-" },
        { t: "Unsettled", num: 1, f: (a) => money(a.unsettled) }], o.cash.accounts))];
  },
  async "Transactions"() {
    const q = new URLSearchParams(window.location.hash.slice(1));
    const data = await api("/api/transactions?" + new URLSearchParams({ q: q.get("q") || "", classification: q.get("c") || "", reconciliation: q.get("r") || "", limit: 200 }));
    const search = h("input", { placeholder: "Search description / counterparty", value: q.get("q") || "" });
    const cls = h("select", {}, ["", "uncategorised", "proposed", "approved"].map((v) => h("option", { value: v, selected: v === (q.get("c") || "") }, v || "any classification")));
    const rec = h("select", {}, ["", "unreconciled", "matched", "exception", "reconciled"].map((v) => h("option", { value: v, selected: v === (q.get("r") || "") }, v || "any reconciliation")));
    const go = h("button", { onclick: () => { window.location.hash = new URLSearchParams({ q: search.value, c: cls.value, r: rec.value }).toString(); render("Transactions"); } }, "Filter");
    return [h("h1", {}, "Transactions"), h("div", { class: "row" }, search, cls, rec, go, h("span", { class: "muted" }, data.total + " matching")),
      table([{ t: "ID", f: (r) => r.record_id }, { t: "Date", f: (r) => r.transaction_date }, { t: "Description", wrap: 1, f: (r) => r.description }, { t: "Counterparty", f: (r) => r.counterparty || "-" },
        { t: "Amount", num: 1, f: (r) => money(r.amount) }, { t: "Kind", f: (r) => r.kind }, { t: "Category", f: (r) => r.category || "-" }, { t: "Class.", f: (r) => statusPill(r.classification_status) },
        { t: "Recon.", f: (r) => statusPill(r.reconciliation_status) }, { t: "Status", f: (r) => statusPill(r.status) }, { t: "Flags", f: (r) => r.open_flags || "" }, { t: "Source", f: (r) => r.source_key },
        { t: "", f: (r) => r.classification_status !== "approved" ? h("button", { class: "small", onclick: () => classify(r) }, "Classify") : "" }], data.items)];
  },
  async "Revenue"() {
    const r = await api("/api/revenue");
    const d = (k) => h("span", { title: r.definitions[k] || "" }, "");
    return [h("h1", {}, "Revenue"), h("p", { class: "muted" }, `Period ${r.period.from} to ${r.period.to}. Revenue is from approved invoices; cash receipts and customer funds are shown separately and are not revenue.`),
      h("div", { class: "grid" }, stat("Gross revenue", money(r.gross_revenue), r.definitions.gross_revenue), stat("Refunds", money(r.refunds)), stat("Net revenue", money(r.net_revenue), r.definitions.net_revenue),
        stat("Processor fees", money(r.processor_fees)), stat("Contribution margin", money(r.contribution_margin), r.definitions.contribution_margin),
        stat("Cash receipts (not revenue)", money(r.cash_receipts_not_revenue)), stat("Customer funds (not revenue)", money(r.customer_funds_not_revenue)), stat("Receivables outstanding", money(r.receivables_outstanding))),
      section("By product", table([{ t: "Product", f: (x) => x[0] }, { t: "Net invoiced", num: 1, f: (x) => money(x[1]) }], Object.entries(r.by_product))),
      section("By customer", table([{ t: "Customer", f: (x) => x[0] }, { t: "Net invoiced", num: 1, f: (x) => money(x[1]) }], Object.entries(r.by_customer))),
      section("By period", table([{ t: "Period", f: (x) => x[0] }, { t: "Net invoiced", num: 1, f: (x) => money(x[1]) }], Object.entries(r.by_period))),
      section("Overdue invoices", table([{ t: "Customer", f: (x) => x.counterparty }, { t: "Number", f: (x) => x.number }, { t: "Due", f: (x) => x.due_date }, { t: "Amount", num: 1, f: (x) => money(x.amount) }], r.overdue_invoices)),
      section("Payment events", h("p", {}, `${r.payment_events.distinct_payments} distinct payments, ${r.payment_events.failed} failed, ${r.payment_events.succeeded_unsettled} succeeded but unsettled. `), h("p", { class: "muted" }, r.payment_events.note))];
  },
  async "Expenses"() {
    const e = await api("/api/expenses");
    return [h("h1", {}, "Expenses & payables"),
      section("Upcoming payables (30 days)", table([{ t: "Vendor", f: (x) => x.counterparty }, { t: "No.", f: (x) => x.number }, { t: "Due", f: (x) => x.due_date || "-" }, { t: "Overdue", f: (x) => x.overdue ? pill("overdue", "bad") : "" }, { t: "Amount", num: 1, f: (x) => money(x.amount) }], e.upcoming.items)),
      section("Payable invoices", table([{ t: "Vendor", f: (x) => x.counterparty }, { t: "No.", f: (x) => x.number }, { t: "Issued", f: (x) => x.issue_date }, { t: "Amount", num: 1, f: (x) => money(x.amount) }, { t: "Approval", f: (x) => statusPill(x.approval_status) }, { t: "Status", f: (x) => x.status }], e.payable_invoices)),
      section("Receipts", table([{ t: "Vendor", f: (x) => x.vendor }, { t: "Date", f: (x) => x.receipt_date }, { t: "Amount", num: 1, f: (x) => money(x.amount) }, { t: "File", f: (x) => x.file_ref || "-" }], e.receipts)),
      section("Recurring vendors", table([{ t: "Vendor", f: (x) => x.vendor }, { t: "Months", f: (x) => x.months_seen }, { t: "Avg", num: 1, f: (x) => money(x.average) }, { t: "Stable", f: (x) => x.stable ? "yes" : "no" }], e.recurring)),
      section("Savings observations (evidence only)", listNote(e.savings_observations.map((s) => s.detail)))];
  },
  async "Cash Flow"() {
    const c = await api("/api/cashflow");
    const base = c.forecasts.base;
    return [h("h1", {}, "Cash flow"),
      h("div", { class: "grid" }, stat("Verified operating cash", money(c.cash.available_operating_cash), c.cash.note),
        stat("Runway", c.runway.runway_months != null ? h("span", {}, c.runway.runway_months + " months") : h("span", { class: "unav" }, c.runway.status.replace(/_/g, " ")), c.runway.note),
        stat("Net burn / month", money(c.runway.net_burn), "Estimate (3-month average)")),
      section("13-week forecast (base). Opening: " + base.opening_cash_basis, h("p", { class: "muted" }, base.uncertainty),
        table([{ t: "Wk", f: (w) => w.week }, { t: "Start", f: (w) => w.week_start }, { t: "Inflow", num: 1, f: (w) => fmt(w.inflow) }, { t: "Outflow", num: 1, f: (w) => fmt(w.outflow) }, { t: "Net", num: 1, f: (w) => fmt(w.net) },
          { t: "Closing cash", num: 1, f: (w) => w.closing_cash == null ? h("span", { class: "unav" }, "Unavailable") : fmt(w.closing_cash) }, { t: "Basis", f: (w) => Object.keys(w.by_basis).join(", ") }], base.weeks)),
      section("Shortfalls", base.shortfalls.length ? table([{ t: "Week", f: (s) => s.week }, { t: "Start", f: (s) => s.week_start }, { t: "Note", wrap: 1, f: (s) => s.note }], base.shortfalls) : h("p", { class: "muted" }, "No shortfall flagged in the base scenario (not a guarantee).")),
      section("Scenarios (end of week 13)", table([{ t: "Scenario", f: (s) => s[0] }, { t: "Closing cash", num: 1, f: (s) => s[1].weeks[12].closing_cash == null ? h("span", { class: "unav" }, "Unavailable") : fmt(s[1].weeks[12].closing_cash) },
        { t: "Cumulative net", num: 1, f: (s) => fmt(s[1].weeks.reduce((a, w) => a + w.net, 0)) }, { t: "Assumptions", wrap: 1, f: (s) => `revenue x${s[1].assumptions.revenue_factor}, costs x${s[1].assumptions.cost_factor}` }], Object.entries(c.forecasts))),
      section("Line items by basis (actual / scheduled / estimate / assumption)", table([{ t: "Date", f: (i) => i.date }, { t: "Item", wrap: 1, f: (i) => i.label }, { t: "Basis", f: (i) => pill(i.basis, i.basis === "assumption" ? "warn" : "") }, { t: "Amount", num: 1, f: (i) => fmt(i.amount_minor) }], base.items)),
      section("Excluded (other currency, no FX method)", table([{ t: "Item", wrap: 1, f: (i) => i.label }, { t: "Currency", f: (i) => i.currency }], base.excluded_items))];
  },
  async "Budgets"() {
    const b = await api("/api/budgets");
    return [h("h1", {}, "Budgets " + b.period), h("p", { class: "muted" }, b.note),
      table([{ t: "Dept", f: (r) => r.department }, { t: "Category", f: (r) => r.category }, { t: "Budget", num: 1, f: (r) => money(r.budget) }, { t: "Actual (approved)", num: 1, f: (r) => money(r.actual_approved) },
        { t: "Unreviewed", num: 1, f: (r) => money(r.unreviewed_proposed) }, { t: "Variance", num: 1, f: (r) => money(r.variance) }, { t: "Var %", num: 1, f: (r) => r.variance_pct == null ? "-" : r.variance_pct + "%" },
        { t: "Alert", f: (r) => r.over_spend_limit ? pill("over limit", "bad") : r.over_budget ? pill("over budget", "warn") : "" }], b.rows)];
  },
  async "Accounting"() {
    const a = await api("/api/accounting");
    return [h("h1", {}, "Accounting"), a.coa_approved_by_accountant ? null : h("p", { class: "pill warn" }, "Chart of accounts is a DRAFT pending accountant approval"),
      section("Journal entries", table([{ t: "ID", f: (e) => e.id }, { t: "Date", f: (e) => e.entry_date }, { t: "Memo", wrap: 1, f: (e) => e.memo }, { t: "Kind", f: (e) => e.kind }, { t: "Total", num: 1, f: (e) => money(e.total) },
        { t: "Status", f: (e) => statusPill(e.status) }, { t: "By", f: (e) => e.created_by }, { t: "", f: (e) => e.status === "draft" ? h("button", { class: "small", onclick: () => act(() => api(`/api/journal/${e.id}/post`, {}), "Posted") }, "Post") : e.status === "posted" ? h("button", { class: "small ghost", onclick: () => reverse(e) }, "Reverse") : "" }], a.entries)),
      section("Periods", table([{ t: "Period", f: (p) => p.id }, { t: "Status", f: (p) => statusPill(p.status) }, { t: "", f: (p) => p.status === "open" ? h("button", { class: "small ghost", onclick: () => act(() => api(`/api/periods/${p.id}/close-request`, {}), "Close requested (needs approval)") }, "Request close") : "" }], a.periods)),
      section("Reconciliation reports", table([{ t: "Account", f: (r) => r.account_id }, { t: "Period", f: (r) => r.period }, { t: "Status", f: (r) => statusPill(r.status) }, { t: "Difference", num: 1, f: (r) => r.difference_minor == null ? h("span", { class: "unav" }, "Unavailable") : fmt(r.difference_minor) },
        { t: "Reasons", wrap: 1, f: (r) => JSON.parse(r.reasons_json).join("; ") || "none" }], a.reconciliations)),
      section("Trial balance (posted)", table([{ t: "Code", f: (r) => r.code }, { t: "Account", f: (r) => r.name }, { t: "Ccy", f: (r) => r.currency }, { t: "Debit", num: 1, f: (r) => fmt(r.dr) }, { t: "Credit", num: 1, f: (r) => fmt(r.cr) }], a.trial_balance)),
      section("Chart of accounts", table([{ t: "Code", f: (r) => r.code }, { t: "Name", f: (r) => r.name }, { t: "Type", f: (r) => r.type }], a.coa))];
  },
  async "Tax & Compliance"() {
    const t = await api("/api/tax");
    const g = t.gst_threshold;
    return [h("h1", {}, "Tax & compliance (Singapore)"), h("p", { class: "pill warn" }, t.disclaimer),
      h("div", { class: "row" }, h("button", { onclick: () => act(() => api("/api/tax/build", {}), "Calendar refreshed") }, "Rebuild calendar from verified facts")),
      section("Company facts (nothing is assumed)", table([{ t: "Fact", f: (f) => f.key }, { t: "Value", f: (f) => f.value == null ? h("span", { class: "unav" }, "Unknown") : f.value }, { t: "Verified", f: (f) => f.verified ? pill("verified", "ok") : pill("unverified", "warn") }, { t: "Source", f: (f) => f.source || "-" }], t.facts)),
      section("Open questions", table([{ t: "Topic", f: (q) => q.topic }, { t: "Question", wrap: 1, f: (q) => q.question }], t.open_questions)),
      section("Filing calendar (potentially applicable)", table([{ t: "Due", f: (i) => i.due_date || "-" }, { t: "Obligation", wrap: 1, f: (i) => i.title }, { t: "Basis", wrap: 1, f: (i) => i.basis }, { t: "Status", f: (i) => i.status }, { t: "Professional review", f: (i) => pill(i.professional_review, i.professional_review === "required" ? "warn" : "ok") }], t.calendar)),
      section("GST threshold monitor (estimate)", h("p", {}, "Trailing 12m invoiced: ", money(g.trailing_12m_invoiced), " of ", money(g.threshold), ` (${g.pct_of_threshold}%). Registration status: ${g.registration_status}${g.verified ? " (verified)" : " (unverified)"}.`), h("p", { class: "muted" }, g.caveat)),
      section("Expenses without supporting documents", table([{ t: "ID", f: (m) => m.record_id }, { t: "Date", f: (m) => m.date }, { t: "Counterparty", f: (m) => m.counterparty || "-" }, { t: "Amount", num: 1, f: (m) => money(m.amount) }], t.missing_documentation))];
  },
  async "Financial Planning"() {
    const p = await api("/api/planning");
    const s = p.scenarios;
    const body = s.status === "ok" ? [h("p", { class: "muted" }, s.uncertainty), table([{ t: "Scenario", f: (r) => r[0] }, { t: "12m revenue", num: 1, f: (r) => fmt(r[1].total_revenue) }, { t: "12m cumulative net", num: 1, f: (r) => fmt(r[1].cumulative_net) }], Object.entries(s.scenarios)),
      section("Most material assumptions (±10% swing in 12m net)", table([{ t: "Assumption", f: (x) => x.assumption }, { t: "−10%", num: 1, f: (x) => fmt(x.delta_if_minus_10pct) }, { t: "+10%", num: 1, f: (x) => fmt(x.delta_if_plus_10pct) }, { t: "Swing", num: 1, f: (x) => fmt(x.swing) }], s.most_material_assumptions))]
      : [h("p", { class: "pill warn" }, "Insufficient data - no figures invented. Needed inputs: " + (s.needed_inputs || []).join(", ")), h("p", { class: "muted" }, s.note || "")];
    const u = p.unit_economics;
    return [h("h1", {}, "Financial planning"), ...body,
      section("Unit economics " + u.period, h("div", { class: "grid" }, stat("Gross margin", u.gross_margin_pct == null ? h("span", { class: "unav" }, "Unavailable") : u.gross_margin_pct + "%", u.gross_margin_note), stat("CAC", money(u.cac), u.cac_note || u.cac_basis))),
      section("Saved forecasts", table([{ t: "ID", f: (f) => f.id }, { t: "Name", f: (f) => f.name }, { t: "As of", f: (f) => f.as_of }, { t: "By", f: (f) => f.created_by }], p.forecasts_saved))];
  },
  async "Investor Reporting"() {
    const i = await api("/api/investor");
    const k = i.kpis;
    return [h("h1", {}, "Investor reporting"), h("p", { class: "pill warn" }, i.banner),
      h("div", { class: "row" }, h("button", { onclick: () => act(() => api("/api/reports/monthly", { period: new Date().toISOString().slice(0, 7) }), "Draft report created") }, "Generate monthly report (draft)")),
      h("div", { class: "grid" }, stat("Net revenue (posted)", money(k.net_revenue)), stat("Contribution margin", money(k.contribution_margin)), stat("Net burn", money(k.net_burn)),
        stat("Runway", k.runway_months != null ? k.runway_months + " months" : h("span", { class: "unav" }, k.runway_status.replace(/_/g, " ")))),
      section("KPI definitions", listNote(Object.entries(k.definitions).map(([a, b]) => a + ": " + b))),
      section("Report versions", table([{ t: "Type", f: (v) => v.report_type }, { t: "Period", f: (v) => v.period }, { t: "Ver", f: (v) => v.version }, { t: "Status", f: (v) => statusPill(v.status) }, { t: "By", f: (v) => v.created_by }, { t: "Approved by", f: (v) => v.approved_by || "-" },
        { t: "", f: (v) => v.status === "draft" ? h("button", { class: "small", onclick: () => act(() => api("/api/reports/approve", { type: v.report_type, period: v.period, version: v.version }), "Report approved") }, "Approve") : "" }], i.versions))];
  },
  async "Connectors"() {
    const c = await api("/api/connectors");
    return [h("h1", {}, "Connectors"), h("p", { class: "muted" }, c.note),
      table([{ t: "Connector", f: (x) => x.name }, { t: "Status", f: (x) => statusPill(x.status) }, { t: "Operations", wrap: 1, f: (x) => x.supported_ops }, { t: "Permissions", wrap: 1, f: (x) => x.permissions }, { t: "Sync", wrap: 1, f: (x) => x.sync_method },
        { t: "Auth", wrap: 1, f: (x) => x.auth }, { t: "Errors/retry", wrap: 1, f: (x) => x.error_handling }, { t: "Limitations", wrap: 1, f: (x) => x.limitations }, { t: "Last success", f: (x) => x.last_success_at || h("span", { class: "unav" }, "never") },
        { t: "Last error", f: (x) => x.last_error || "-" }], c.connectors), h("p", {}, "Stripe webhook secret configured: ", pill(c.webhook_secret_configured ? "yes" : "no (events rejected)", c.webhook_secret_configured ? "ok" : "warn"))];
  },
  async "Approvals"() {
    const a = await api("/api/approvals"), f = await api("/api/flags");
    return [h("h1", {}, "Approvals & review queue"),
      section("Pending approvals", table([{ t: "ID", f: (x) => x.id }, { t: "Type", f: (x) => x.type }, { t: "Amount", num: 1, f: (x) => x.amount ? money(x.amount) : "-" }, { t: "By", f: (x) => x.requested_by }, { t: "Detail", wrap: 1, f: (x) => JSON.stringify(x.payload) },
        { t: "", f: (x) => h("span", {}, h("button", { class: "small", onclick: () => decide(x, true) }, "Approve"), " ", h("button", { class: "small bad", onclick: () => decide(x, false) }, "Reject")) }], a.items)),
      section("Proposed matches", table([{ t: "ID", f: (m) => m.id }, { t: "Txn", f: (m) => m.transaction_id }, { t: "Doc", f: (m) => m.doc_type + " #" + m.doc_id }, { t: "Score", f: (m) => m.score }, { t: "Reason", f: (m) => m.reason },
        { t: "", f: (m) => h("span", {}, h("button", { class: "small", onclick: () => act(() => api(`/api/matches/${m.id}/confirm`, { accept: true }), "Match confirmed") }, "Confirm"), " ", h("button", { class: "small ghost", onclick: () => act(() => api(`/api/matches/${m.id}/confirm`, { accept: false }), "Match rejected") }, "Reject")) }], a.pending_matches)),
      section("Open flags", table([{ t: "ID", f: (x) => x.id }, { t: "Severity", f: (x) => sevPill(x.severity) }, { t: "Entity", f: (x) => x.entity_type + " " + x.entity_id }, { t: "Code", f: (x) => x.code }, { t: "Detail", wrap: 1, f: (x) => x.detail },
        { t: "", f: (x) => h("button", { class: "small ghost", onclick: () => { const n = prompt("Resolution note (required)"); if (n) act(() => api(`/api/flags/${x.id}/resolve`, { note: n }), "Flag resolved"); } }, "Resolve") }], f.items))];
  },
  async "Audit Centre"() {
    const a = await api("/api/audit");
    return [h("h1", {}, "Audit centre"), h("p", {}, "Hash chain: ", pill(a.chain_ok ? "intact" : "BROKEN at entry " + a.broken_at, a.chain_ok ? "ok" : "bad")),
      section("Activity log", table([{ t: "#", f: (x) => x.id }, { t: "Time", f: (x) => x.ts }, { t: "Actor", f: (x) => x.actor }, { t: "Action", f: (x) => x.action }, { t: "Entity", f: (x) => (x.entity || "") + " " + (x.entity_id || "") }, { t: "Detail", wrap: 1, f: (x) => x.after_json || "" }], a.items)),
      section("Imports", table([{ t: "ID", f: (x) => x.id }, { t: "Source", f: (x) => x.source_type }, { t: "File", f: (x) => x.filename }, { t: "Rows", f: (x) => x.rows_total }, { t: "Imported", f: (x) => x.rows_imported }, { t: "Dup. source", f: (x) => x.rows_duplicate_source }, { t: "Rejected", f: (x) => x.rows_rejected }, { t: "By", f: (x) => x.imported_by }, { t: "At", f: (x) => x.imported_at }], a.imports)),
      section("Rejected import rows", table([{ t: "Batch", f: (x) => x.batch_id }, { t: "Row", f: (x) => x.row_number }, { t: "Reason", wrap: 1, f: (x) => x.reason }], a.rejects)),
      section("Orchestrator tasks", table([{ t: "ID", f: (x) => x.id }, { t: "Type", f: (x) => x.type }, { t: "Status", f: (x) => statusPill(x.status) }, { t: "Attempts", f: (x) => x.attempts }, { t: "Error", wrap: 1, f: (x) => x.error || "" }, { t: "By", f: (x) => x.requested_by }], a.tasks))];
  },
};

const fmt = (minor) => (minor < 0 ? "-" : "") + Math.abs(minor / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function classify(r) {
  const cat = prompt(`Category code for #${r.record_id} (${r.description}). Current: ${r.category || "none"}`, r.category || "");
  if (cat) act(() => api(`/api/transactions/${r.record_id}/classify`, { category: cat }), "Classified");
}
function decide(x, approve) { const note = prompt(approve ? "Approval note (optional)" : "Reason for rejection") || ""; act(() => api(`/api/approvals/${x.id}/decide`, { approve, note }).then((r) => { if (r.result && r.result.token_shown_once) alert("New user token (shown once):\n" + r.result.token_shown_once); return r; }), approve ? "Approved" : "Rejected"); }
function reverse(e) { const reason = prompt("Reason for reversal"); if (reason) act(() => api(`/api/journal/${e.id}/reverse`, { date: new Date().toISOString().slice(0, 10), reason }), "Reversal drafted (needs posting by a second person)"); }

function importControls() {
  const acct = h("input", { placeholder: "account id", size: 8 }), file = h("input", { type: "file", accept: ".csv" });
  return h("div", { class: "row" }, h("strong", {}, "Import bank CSV:"), acct, file, h("button", { onclick: async () => {
    if (!file.files[0] || !acct.value) return toast("Choose an account id and a CSV file");
    const content = await file.files[0].text();
    act(() => api("/api/import/csv", { account_id: Number(acct.value), content, filename: file.files[0].name, period: new Date().toISOString().slice(0, 7) }), "Import workflow finished - check Audit Centre");
  } }, "Import"));
}

async function render(name) {
  current = name;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.s === name));
  const main = $("main");
  if (!token) { main.replaceChildren(h("p", { class: "muted" }, "Sign in with an API token to load data.")); return; }
  try {
    const nodes = await VIEWS[name]();
    main.replaceChildren(...nodes.filter(Boolean));
    if (name === "Transactions") main.insertBefore(importControls(), main.children[1]);
    $("nav").classList.remove("open");
  } catch (e) {
    main.replaceChildren(h("p", { class: "pill bad" }, e.message));
    if (/authentication/.test(e.message)) { token = null; }
  }
}

function boot() {
  $("nav").replaceChildren(...SECTIONS.map((s) => h("a", { "data-s": s, onclick: () => render(s) }, s)));
  $("menu").addEventListener("click", () => $("nav").classList.toggle("open"));
  $("login").addEventListener("click", () => {
    const t = prompt("Paste your API token");
    if (t) { token = t.trim(); try { sessionStorage.setItem("kff_token", token); } catch (e) { /* storage unavailable */ } api("/api/overview").then((o) => { $("who").textContent = o.user.name + " (" + o.user.role + ")"; render(current); }).catch((e) => { token = null; toast(e.message); }); }
  });
  if (token) api("/api/overview").then((o) => { $("who").textContent = o.user.name + " (" + o.user.role + ")"; render("Overview"); }).catch(() => { token = null; });
  else render("Overview");
}
boot();
