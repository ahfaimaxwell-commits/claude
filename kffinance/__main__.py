"""CLI: init / serve / seed-synthetic / backup / verify-audit."""
import argparse
import os
import sqlite3
import sys

from . import db as dbm
from .core import create_user, verify_audit_chain

DEFAULT_DB = "data/kffinance.db"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kffinance")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("init", "serve", "seed-synthetic", "verify-audit"):
        p = sub.add_parser(name)
        p.add_argument("--db", default=os.environ.get("KFF_DB", DEFAULT_DB))
        if name == "init":
            p.add_argument("--admin", default="founder")
        if name == "serve":
            p.add_argument("--host", default="127.0.0.1")
            p.add_argument("--port", type=int, default=8800)
    b = sub.add_parser("backup")
    b.add_argument("--db", default=os.environ.get("KFF_DB", DEFAULT_DB))
    b.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "init":
        os.makedirs(os.path.dirname(os.path.abspath(a.db)), exist_ok=True)
        c = dbm.connect(a.db)
        dbm.init_db(c)
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            print("Database already initialised; no new user created.")
            return
        tok = create_user(c, a.admin, "finance_admin")
        print(f"Initialised {a.db}\nAdmin '{a.admin}' API token (shown ONCE, store it in a password manager):\n{tok}")
    elif a.cmd == "serve":
        from .server import serve
        c = dbm.connect(a.db); dbm.init_db(c); c.close()
        serve(a.db, a.host, a.port)
    elif a.cmd == "seed-synthetic":
        if os.path.abspath(a.db) == os.path.abspath(DEFAULT_DB):
            sys.exit("Refusing to load synthetic data into the default (production) database. Pass a separate --db path.")
        from . import orchestrator as o, synthetic as s
        from .core import User
        os.makedirs(os.path.dirname(os.path.abspath(a.db)), exist_ok=True)
        c = dbm.connect(a.db); dbm.init_db(c)
        tokens = {n: create_user(c, n, r) for n, r in (("admin", "finance_admin"), ("approver", "approver"), ("preparer", "preparer"), ("viewer", "viewer"))}
        s.mark_synthetic(c)
        prep = User(0, "preparer", "preparer")
        d = s.load_basic(c, None, prep)
        o.submit_import_workflow(c, prep, "csv", d["accounts"]["Operating bank"], s.BANK_CSV, "2026-09", "synthetic_bank.csv")
        print(o.run_pending(c))
        print("SYNTHETIC database ready. Tokens:")
        for n, t in tokens.items():
            print(f"  {n}: {t}")
    elif a.cmd == "verify-audit":
        ok, bad = verify_audit_chain(dbm.connect(a.db))
        print("audit chain OK" if ok else f"audit chain BROKEN at id {bad}")
        sys.exit(0 if ok else 1)
    elif a.cmd == "backup":
        src = sqlite3.connect(a.db)
        dst = sqlite3.connect(a.out)
        src.backup(dst)
        dst.close(); src.close()
        print(f"Backup written to {a.out}")


if __name__ == "__main__":
    main()
