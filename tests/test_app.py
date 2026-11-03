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
os.environ["ANTHROPIC_API_ROOT"] = "http://127.0.0.1:8098"

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

    def test_auto_mode(self):
        w = watch(terms="Canon SX110\niPod Classic", match_mode="auto")
        self.assertTrue(app.matches(w, "Canon PowerShot SX110 IS"))
        self.assertFalse(app.matches(w, "Canon PowerShot SX1100"))
        self.assertTrue(app.matches(w, "Apple iPod Classic 80GB"))
        self.assertFalse(app.matches(w, "Apple iPod Nano"))
        short = watch(terms="Canon A40\nPSP 3000\nAGS-101\nCanon G12", match_mode="auto")
        self.assertTrue(app.matches(short, "Canon PowerShot A40 2MP"))
        self.assertFalse(app.matches(short, "Samsung Galaxy A40 phone"))
        self.assertTrue(app.matches(short, "Sony PSP-3000 Piano Black"))
        self.assertFalse(app.matches(short, "Hoover WindTunnel 3000"))
        self.assertTrue(app.matches(short, "Game Boy Advance SP AGS-101 Pearl"))
        self.assertTrue(app.matches(short, "CANON G12 10MP"))
        self.assertFalse(app.matches(short, "Logitech G12 keyboard"))

    def test_search_text(self):
        self.assertEqual(app.search_text_for("Canon SX110"), "SX110")
        self.assertEqual(app.search_text_for("Sony DSC-W7"), "DSC-W7")
        self.assertEqual(app.search_text_for("Olympus FE-280"), "FE-280")
        self.assertEqual(app.search_text_for("Coolpix 2500"), "Coolpix 2500")   # bare number: too generic alone
        self.assertEqual(app.search_text_for("iPod Classic"), "iPod Classic")

    def test_filter_parsing(self):
        f = app.norm_filters({"cat": ["1", "2", "1"], "flagged": ["0", "1"], "model": "5", "q": [""]})
        self.assertEqual(f, {"cat": ["1", "2"], "flagged": "1", "watch": ["5"]})

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
        return {r["item_id"] for r in app.rows({k: [str(x) for x in v] if isinstance(v, list) else str(v)
                                                 for k, v in f.items()})}

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
        with app.db() as con:
            ks = {r["searched"]: dict(r) for r in con.execute("SELECT * FROM keyword_stats")}
        self.assertEqual((ks["sx110"]["results"], ks["sx110"]["matched"]), (4, 3))   # case-only skipped; 105 from the 1-cent pool
        self.assertEqual(ks["sx100"]["results"], 0)

    def test_filters(self):
        self.assertEqual(self.ids(cat=self.ipods), {"401", "404"})
        self.assertEqual(self.ids(cat=self.ipods, buynow=0), {"401"})
        self.assertEqual(self.ids(cat=self.ipods, ending=24), {"401"})
        self.assertEqual(self.ids(q="sx110", flagged=0), {"101", "105"})
        self.assertEqual(self.ids(max=26), {"101", "102", "105"})
        self.assertEqual(self.ids(tier="try"), set())
        self.assertEqual(self.ids(cat=[self.ipods, 1], q="ipod"), {"401", "404"})
        ends = [r["end_utc"] for r in app.rows({"over": "1"})]
        self.assertEqual(ends, sorted(ends))   # ending soonest is the default sort
        room = [r["item_id"] for r in app.rows({"sort": "room"})]
        heads = [r["headroom"] for r in app.rows({"sort": "room"})]
        self.assertEqual(heads, sorted(heads, reverse=True), room)

    def test_shipping(self):
        # 105 only exists in the 1-cent pool; 101 ships $11.99; 102 has no quote (estimate never counts).
        self.assertEqual(self.ids(ship="promo"), {"105"})
        self.assertEqual(self.ids(ship="15", over=1, cat=[1]), {"101", "105", "202"})
        self.assertEqual(self.ids(ship="5"), {"105"})
        self.assertEqual(self.ids(shipmax="12", over=1, cat=[1]), {"101", "105"})
        self.assertNotIn("102", self.ids(ship="15", over=1))
        rs = app.rows({"sort": "ship", "over": "1"})
        self.assertEqual(rs[0]["item_id"], "105")
        self.assertFalse(rs[-1]["ship_known"])        # unquoted sort last
        self.assertIn("1¢ shipping", app.results_page({"ship": ["promo"]}))

    def test_locations(self):
        with app.db() as con:
            s = dict(con.execute("SELECT * FROM sellers WHERE id='999'").fetchone())
            self.assertEqual((s["city"], s["state"], s["combine_ok"], s["local"]), ("Dallas", "TX", 0, 0))
            self.assertEqual(con.execute("SELECT local FROM sellers WHERE id='43'").fetchone()[0], 1)  # seeded drivable
        self.assertEqual(self.ids(local=1, over=1), {"101", "102", "105", "401"})
        self.assertEqual(self.ids(state="OR"), {"404"})
        self.assertEqual(self.ids(seller="999", over=1), {"202", "301"})
        rs = {r["item_id"]: r for r in app.rows({"over": "1"})}
        self.assertEqual(rs["101"]["seller_live"], 4)   # 101, 102, 105, 401 from seller 43

    def test_good_buys(self):
        # 101: clean, quoted, room $19; 401: room $10; 404: buy now room $6; 102 flagged.
        self.assertEqual(self.ids(good=1, ending=9999), {"101", "105", "401", "404"})
        self.assertEqual(self.ids(good=1), {"101", "105", "401"})            # default window: 24h
        self.assertEqual(self.ids(good=1, minroom=15), {"101", "105"})

    def test_facets(self):
        rs = app.live_rows()
        f = app.norm_filters({"state": "OR"})
        counts = app.facet_counts(rs, f, "state", lambda r: r["state"] or "?")
        self.assertEqual(counts.get("TX"), 4)    # other states still counted while OR is selected

    def test_pages_render(self):
        for q in ({}, {"group": ["location"]}, {"seller": ["43"], "over": ["1"]}, {"good": ["1"]}, {"group": ["day"]},
                  {"group": ["none"], "cat": ["999"]}):
            html = app.results_page(q)
            self.assertIn("<form id=ff", html)
        self.assertIn("more from this seller", app.results_page({"over": ["1"]}))
        self.assertIn("Goodwill Industries of Central Texas", app.locations_page({}))


class BidsAndSuggestions(unittest.TestCase):
    """Runs after ScanAgainstMock (alphabetical order is not relied on: it scans itself if needed)."""

    @classmethod
    def setUpClass(cls):
        cls.mock = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_sgw.py")])
        time.sleep(0.8)
        app.init()
        if not app.rows({"over": "1"}):
            app.run_scan("test")

    @classmethod
    def tearDownClass(cls):
        cls.mock.terminate()
        cls.mock.wait()

    def setUp(self):
        with app.db() as con:
            for t in ("bids", "suggestions", "seller_items", "ai_seen", "ai_usage", "sweeps"):
                con.execute(f"DELETE FROM {t}")
            con.execute("UPDATE settings SET value='' WHERE key='anthropic_key'")

    def set_key(self, k):
        with app.db() as con:
            con.execute("INSERT INTO settings VALUES ('anthropic_key', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k,))

    def test_track_and_sweep_without_ai(self):
        ok, msg, row = app.track("https://shopgoodwill.com/item/101", "test", "15")
        self.assertTrue(ok, msg)
        self.assertEqual(row["seller_id"], "43")
        self.assertEqual(app.sweep_seller("43"), "ok")
        with app.db() as con:
            sug = {r["item_id"]: dict(r) for r in con.execute("SELECT * FROM suggestions")}
            bid = dict(con.execute("SELECT * FROM bids WHERE item_id='101'").fetchone())
        self.assertEqual(bid["detected_amount"], 15.0)
        self.assertEqual(sug["503"]["kind"], "watch")     # SX120 matches the Canon SX watch
        self.assertNotIn("101", sug)                       # the one you're bidding on isn't suggested back
        self.assertNotIn("504", sug)                       # "case only" skip word
        self.assertNotIn("505", sug)                       # ends long after the bid: can't ship together
        self.assertFalse(any(g["kind"] == "ai" for g in sug.values()))  # no key, no AI

    def test_ai_picks_and_cache(self):
        self.set_key("test-key")
        app.track("101", "test")
        self.assertEqual(app.sweep_seller("43"), "ok")
        with app.db() as con:
            ai_rows = {r["item_id"]: dict(r) for r in con.execute("SELECT * FROM suggestions WHERE kind='ai'")}
            calls = con.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0]
        self.assertEqual(set(ai_rows), {"501"})          # Game Boy picked; 999999 (never sent) ignored
        self.assertIn("Nintendo", ai_rows["501"]["reason"])
        self.assertEqual(calls, 1)
        app.sweep_seller("43")                           # second sweep: titles already seen, no new call
        with app.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM suggestions WHERE kind='ai'").fetchone()[0], 1)
        self.assertGreater(app.ai_spend()["titles"], 0)

    def test_bad_key_is_reported_not_silent(self):
        self.set_key("wrong")
        app.track("101", "test")
        self.assertEqual(app.sweep_seller("43"), "partial")
        with app.db() as con:
            w = dict(con.execute("SELECT * FROM sweeps WHERE seller_id='43'").fetchone())
            self.assertGreater(con.execute("SELECT COUNT(*) FROM suggestions WHERE kind='watch'").fetchone()[0], 0)
        self.assertIn("API key was refused", w["error"])

    def test_track_rejects_junk(self):
        self.assertFalse(app.track("not a link", "test")[0])

    def test_track_unknown_listing_reads_detail(self):
        ok, msg, row = app.track("501", "test")
        self.assertTrue(ok, msg)
        self.assertEqual(row["title"], "Nintendo Game Boy Color Atomic Purple")

    def test_import(self):
        data = [{"category": "Game systems", "label": "Game Boy Color", "keywords": ["Game Boy Color", "GBC"],
                 "match_mode": "auto", "match": "", "exclude": "case", "max_buy": 30, "tier": "try", "notes": "x", "enabled": True},
                {"category": "Cameras", "label": "Kodak Zi8", "keywords": ["Kodak Zi8"], "max_buy": 12},
                {"label": "broken"}]
        dry = app.import_watches(data)
        self.assertEqual((len(dry["add"]), len(dry["skip"]), len(dry["errors"])), (1, 1, 1))
        self.assertEqual(dry["new_categories"], ["Game systems"])
        with app.db() as con:
            self.assertIsNone(con.execute("SELECT 1 FROM categories WHERE name='Game systems'").fetchone())  # preview wrote nothing
        app.import_watches(data[:2], apply=True)
        with app.db() as con:
            r = con.execute("SELECT m.terms, c.name FROM models m JOIN categories c ON c.id=m.category_id WHERE label='Game Boy Color'").fetchone()
        self.assertEqual((r[0], r[1]), ("Game Boy Color\nGBC", "Game systems"))
        app.import_watches([dict(data[0], search=["GBC", "Game Boy"])], replace=True, apply=True)
        with app.db() as con:
            self.assertEqual(con.execute("SELECT search FROM models WHERE label='Game Boy Color'").fetchone()[0], "GBC\nGame Boy")
        again = app.import_watches(data[:2], apply=True)
        self.assertEqual(len(again["add"]), 0)

    def test_pages_render(self):
        app.track("101", "test")
        app.sweep_seller("43")

        class H:
            headers = {"Host": "roger.local:8088"}
        html = app.bids_page(H())
        self.assertIn("Also from this seller", html)
        self.assertIn("javascript:", html)
        js = app.userscript(H())
        self.assertIn("@connect      roger.local", js)
        self.assertIn(app.settings()["track_token"], js)
        self.assertIn("★", app.results_page({"over": ["1"]}))
        self.assertIn("Preview", app.import_page())


if __name__ == "__main__":
    unittest.main()
