"""Deterministic SYNTHETIC data for tests and demos. Never loaded into a production database."""
from __future__ import annotations

from .core import tx
from .db import now
from .money import parse_amount


def mark_synthetic(conn):
    with tx(conn):
        conn.execute("INSERT OR REPLACE INTO settings VALUES ('dataset_label','SYNTHETIC TEST DATA',?)", (now(),))


def make_accounts(conn):
    with tx(conn):
        conn.execute("INSERT INTO accounts (name,institution,type,currency,funds_class,created_at) VALUES ('Operating bank (synthetic)','Test Bank','bank','SGD','operating',?)", (now(),))
        conn.execute("INSERT INTO accounts (name,institution,type,currency,funds_class,created_at) VALUES ('Stripe platform (synthetic)','Stripe','payment_processor','SGD','operating',?)", (now(),))
        conn.execute("INSERT INTO accounts (name,institution,type,currency,funds_class,created_at) VALUES ('Customer funds (synthetic)','Test Bank','bank','SGD','customer_funds',?)", (now(),))
    return {r["name"].split(" (")[0]: r["id"] for r in conn.execute("SELECT id,name FROM accounts")}


def add_snapshot(conn, account_id, as_of, minor, ccy="SGD", source="synthetic statement", ref="synthetic-evidence-1", verified=True, by="seed"):
    with tx(conn):
        conn.execute("INSERT INTO balance_snapshots (account_id,as_of,amount_minor,currency,source,evidence_ref,verified,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (account_id, as_of, minor, ccy, source, ref, int(verified), by, now()))


BANK_CSV = """date,description,amount,reference,counterparty
2026-07-03,Payroll July,-4000.00,B001,Payroll
2026-08-03,Payroll August,-4000.00,B002,Payroll
2026-09-03,Payroll September,-4000.00,B003,Payroll
2026-07-05,Supabase subscription,-25.00,B004,Supabase
2026-08-05,Supabase subscription,-25.00,B005,Supabase
2026-09-05,Supabase subscription,-25.00,B006,Supabase
2026-07-06,Lovable credits,-50.00,B007,Lovable
2026-08-06,Lovable credits,-50.00,B008,Lovable
2026-09-06,Lovable credits,-50.00,B009,Lovable
2026-09-12,ACME Consulting Pte Ltd,-1200.00,B010,ACME Consulting
2026-09-12,ACME Consulting Pte Ltd,-1200.00,B011,ACME Consulting
2026-09-15,Receipt from Tan Property platform fee,19.00,B012,Tan Property
2026-09-16,Receipt from Lim Holdings platform fee,218.00,B013,Lim Holdings
2026-09-20,Unknown deposit,500.00,B014,
2026-09-21,Bad row,not-a-number,B015,
"""


def load_basic(conn, admin, preparer):
    """Accounts, a verified opening balance, bank CSV, AR invoices and a receipt. Returns ids."""
    from .agents import payables
    ids = make_accounts(conn)
    add_snapshot(conn, ids["Operating bank"], "2026-06-30", parse_amount("20000.00", "SGD"))
    with tx(conn):
        conn.execute("INSERT INTO receipts (vendor,receipt_date,amount_minor,currency,file_ref,created_at) VALUES ('Lovable',?,?,?,?,?)",
                     ("2026-09-06", parse_amount("50.00", "SGD"), "SGD", "synthetic-receipt-lovable-sep", now()))
        conn.execute("INSERT INTO receipts (vendor,receipt_date,amount_minor,currency,file_ref,created_at) VALUES ('ACME Consulting',?,?,?,?,?)",
                     ("2026-09-12", parse_amount("1200.00", "SGD"), "SGD", "synthetic-receipt-acme", now()))
    r1 = payables.submit_invoice(conn, preparer, "receivable", "Tan Property", "INV-001", "2026-09-01", "2026-09-15", parse_amount("19.00", "SGD"), 0, "SGD", product="platform fee")
    r2 = payables.submit_invoice(conn, preparer, "receivable", "Lim Holdings", "INV-002", "2026-09-01", "2026-09-15", parse_amount("220.00", "SGD"), 0, "SGD", product="visibility promotion")
    return {"accounts": ids, "invoices": [r1, r2]}
