import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kffinance import core, db  # noqa: E402
from kffinance import synthetic as syn  # noqa: E402
from kffinance.core import User  # noqa: E402

ADMIN = User(1, "admin", "finance_admin")
APPROVER = User(2, "approver", "approver")
APPROVER2 = User(3, "approver2", "approver")
PREPARER = User(4, "preparer", "preparer")
VIEWER = User(5, "viewer", "viewer")


def fresh():
    c = db.connect(":memory:")
    db.init_db(c)
    for u in (ADMIN, APPROVER, APPROVER2, PREPARER, VIEWER):
        core.create_user(c, u.name, u.role)
    return c


class Base(unittest.TestCase):
    def setUp(self):
        self.c = fresh()

    def tearDown(self):
        self.c.close()

    def one(self, sql, *a):
        return self.c.execute(sql, a).fetchone()

    def acct(self, name="Bank", ccy="SGD", funds="operating", typ="bank"):
        with core.tx(self.c):
            cur = self.c.execute("INSERT INTO accounts (name,type,currency,funds_class,created_at) VALUES (?,?,?,?,?)", (name, typ, ccy, funds, db.now()))
        return cur.lastrowid

    def snapshot(self, account_id, as_of, minor, verified=True, ccy="SGD"):
        syn.add_snapshot(self.c, account_id, as_of, minor, ccy, verified=verified)
