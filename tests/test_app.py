"""python3 -m unittest discover tests   (from the sgw-watch folder)"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp()
os.environ["SGW_DB"] = os.path.join(TMP, "t.db")
os.environ["SGW_API_ROOT"] = "http://127.0.0.1:8099/api"
os.environ["SGW_DELAY"] = "0"

import app  # noqa: E402
import sgw  # noqa: E402


def watch(**kw):
    m = {"match": "", "match_mode": "model", "exclude": "", "terms": ""}
    m.update(kw)
    return m


class Matching(unittest.TestCase):
    def test_digit_bounded(self):
        self.assertTrue(app.title_matches("Sony DSC-W7 Cybershot", ["w7"]))
        self.assertTrue(app.title_matches("SONY DSCW7", ["w7"]))
        self.assertFalse(app.title_matches("Sony DSC-W70", ["w7"]))
        self.assertFalse(app.title_matches("Fujifilm Finepix F300EXR", ["f30"]))
        self.assertTrue(app.title_matches("Fuji FinePix F30 Zoom", ["f30"]))

    def test_tokens(self):
        self.assertEqual(app.tokens_for(watch(terms="Sony DSC-W7\nCanon SX110\n")), ["w7", "sx110"])
        self.assertEqual(app.tokens_for(watch(match="F31fd, F31 fd", terms="x")), ["f31fd", "f31 fd"])

    def test_modes_and_excludes(self):
        w = watch(terms="iPod Classic 160GB\niPod Classic", match_mode="all", exclude="nano, case")
        self.assertTrue(app.matches(w, "Apple iPod Classic 160GB Black"))
        self.assertTrue(app.matches(w, "Apple iPod (Classic) 80GB"))
        self.assertFalse(app.matches(w, "Apple iPod 5th gen"))
        self.assertFalse(app.matches(w, "iPod Nano Classic look"))
        self.assertTrue(app.matches(watch(terms="zapper", match_mode="search"), "anything at all"))
        self.assertFalse(app.matches(watch(terms="Canon SX110", exclude="parts"), "Canon SX110 for parts"))

    def test_flags(self):
        red = app.words(app.DEFAULTS["red_flags"])
        self.assertIn("parts", app.flags_for("Canon SX110 For Parts", red))
        self.assertIn("lot", app.flags_for("Lot of 3 Kodak Zi8", red))
        self.assertEqual(app.flags_for("Canon SX110 IS tested works", red), [])

    def test_pacific_end_time(self):
        # 2026-07-01 10:00 PDT is 17:00 UTC; reading it as UTC is the old bug.
        self.assertEqual(sgw.parse_end("2026-07-01T10:00:00"), "2026-07-01T17:00:00+00:00")
        self.assertEqual(sgw.parse_end("2026-12-01T10:00:00"), "2026-12-01T18:00:00+00:00")


class Upgrade(unittest.TestCase):
    def test_v1_database_upgrades(self):
        """A database from the first version (no categories) keeps its watches and listings."""
        path = os.path.join(TMP, "v1.db")
        con = sqlite3.connect(path)
        con.executescript("""
          CREATE TABLE models (id INTEGER PRIMARY KEY, label TEXT NOT NULL, terms TEXT NOT NULL,
            match TEXT NOT NULL DEFAULT '', max_buy REAL NOT NULL, tier TEXT NOT NULL DEFAULT 'winner',
            notes TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
          CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          INSERT INTO settings VALUES ('seeded', 'x');
          INSERT INTO models(label, terms, max_buy) VALUES ('Mine', 'Canon G12', 90);
        """)
        con.commit()
        con.close()
        old = app.DB_PATH
        app.DB_PATH = path
        try:
            app.init()
            app.init()  # idempotent
            with app.db() as c:
                rows = c.execute("SELECT m.label, c.name FROM models m JOIN categories c ON c.id=m.category_id").fetchall()
            self.assertEqual([tuple(r) for r in rows], [("Mine", "Cameras")])
        finally:
            app.DB_PATH = old


class ScanAgainstMock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_sgw.py")])
        time.sleep(0.8)
        app.init()
        with app.db() as con:
            cid = con.execute("INSERT INTO categories(name) VALUES ('iPods')").lastrowid
            con.execute("INSERT INTO models(label, terms, match_mode, exclude, max_buy, tier, category_id) "
                        "VALUES ('iPod Classic', 'iPod Classic', 'all', 'nano', 40, 'winner', ?)", (cid,))
        cls.ipods = cid
        cls.msg = app.run_scan("test")

    @classmethod
    def tearDownClass(cls):
        cls.mock.terminate()
        cls.mock.wait()

    def ids(self, **f):
        return {r["item_id"] for r in app.rows({k: str(v) for k, v in f.items()})}

    def test_scan(self):
        self.assertTrue(self.msg.startswith("Scan ok"), self.msg)
        got = {r["item_id"]: r for r in app.rows({"over": "1"})}
        self.assertNotIn("201", got)   # W70 is not a W7
        self.assertNotIn("103", got)   # "case only" is skipped
        r = got["101"]                 # quoted: 14 + 8.50 + 3.49
        self.assertTrue(r["ship_known"])
        self.assertAlmostEqual(r["landed"], 25.99)
        self.assertAlmostEqual(r["bid_up_to"], 45 - 11.99)
        self.assertIn("no shipping quote", got["102"]["flags"])  # declined quote is never clean
        self.assertLess(got["202"]["headroom"], 0)
        self.assertNotIn("202", self.ids())
        self.assertIn("401", got)       # all-words match
        self.assertNotIn("402", got)    # excluded: nano
        self.assertNotIn("403", got)    # no "classic"

    def test_filters(self):
        self.assertEqual(self.ids(cat=self.ipods), {"401", "404"})
        self.assertEqual(self.ids(cat=self.ipods, buynow=0), {"401"})
        self.assertEqual(self.ids(cat=self.ipods, ending=24), {"401"})
        self.assertEqual(self.ids(q="sx110", flagged=0), {"101"})
        self.assertEqual(self.ids(max=26), {"101", "102"})
        self.assertEqual(self.ids(tier="try"), set())
        room = [r["item_id"] for r in app.rows({"sort": "room"})]
        heads = [r["headroom"] for r in app.rows({"sort": "room"})]
        self.assertEqual(heads, sorted(heads, reverse=True), room)


if __name__ == "__main__":
    unittest.main()
