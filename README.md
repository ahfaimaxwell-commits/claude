# KeyFables Finance Command Centre

AI-assisted finance operations for KeyFables (Singapore): ingestion, reconciliation, treasury, revenue,
payables, tax calendar, FP&A and management reporting, behind server-enforced approvals and an audit trail.

Zero dependencies and zero running cost: Python 3.11+ standard library and SQLite.

```
python3 -m kffinance init                         # creates data/kffinance.db and prints the admin token ONCE
python3 -m kffinance serve                        # http://127.0.0.1:8800
python3 -m kffinance seed-synthetic --db /tmp/demo.db   # SYNTHETIC demo data in a separate database
python3 -m kffinance backup --out backup.db
python3 -m kffinance verify-audit
cd tests && python3 -m unittest discover -p "test_*.py"
```

Read [docs/FINANCE_SYSTEM.md](docs/FINANCE_SYSTEM.md) first. **This repository is public: never commit real financial
data, tokens or keys** (`.gitignore` blocks databases and `.env`). Real data belongs in the private operations repo.
