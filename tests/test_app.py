"""python3 -m unittest discover tests   (from the sgw-watch folder)"""
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["SGW_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["SGW_API_ROOT"] = "http://127.0.0.1:8099/api"
os.environ["SGW_DELAY"] = "0"

import app  # noqa: E402
import sgw  # noqa: E402


class Matching(unittest.TestCase):
    def test_digit_bounded(self):
        self.assertTrue(app.title_matches("Sony DSC-W7 Cybershot", ["w7"]))
        self.assertTrue(app.title_matches("SONY DSCW7", ["w7"]))
        self.assertFalse(app.title_matches("Sony DSC-W70", ["w7"]))
        self.assertFalse(app.title_matches("Fujifilm Finepix F300EXR", ["f30"]))
        self.assertTrue(app.title_matches("Fuji FinePix F30 Zoom", ["f30"]))

    def test_tokens(self):
        m = {"match": "", "terms": "Sony DSC-W7\nCanon SX110\n"}
        self.assertEqual(app.tokens_for(m), ["w7", "sx110"])
        self.assertEqual(app.tokens_for({"match": "F31fd, F31 fd", "terms": "x"}), ["f31fd", "f31 fd"])

    def test_flags(self):
        red = app.words(app.DEFAULTS["red_flags"])
        self.assertIn("parts", app.flags_for("Canon SX110 For Parts", red))
        self.assertIn("lot", app.flags_for("Lot of 3 Kodak Zi8", red))
        self.assertEqual(app.flags_for("Canon SX110 IS tested works", red), [])

    def test_pacific_end_time(self):
        # 2026-07-01 10:00 PDT is 17:00 UTC; reading it as UTC is the old bug.
        self.assertEqual(sgw.parse_end("2026-07-01T10:00:00"), "2026-07-01T17:00:00+00:00")
        self.assertEqual(sgw.parse_end("2026-12-01T10:00:00"), "2026-12-01T18:00:00+00:00")


class ScanAgainstMock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_sgw.py")])
        time.sleep(0.8)
        app.init()

    @classmethod
    def tearDownClass(cls):
        cls.mock.terminate()

    def test_scan(self):
        msg = app.run_scan("test")
        self.assertTrue(msg.startswith("Scan ok"), msg)
        got = {r["item_id"]: r for r in app.rows(show_all=True)}
        self.assertNotIn("201", got)   # W70 is not a W7
        self.assertNotIn("103", got)   # "case only" is skipped
        r = got["101"]                 # quoted: 14 + 8.50 + 3.49
        self.assertTrue(r["ship_known"])
        self.assertAlmostEqual(r["landed"], 25.99)
        self.assertAlmostEqual(r["bid_up_to"], 45 - 11.99)
        self.assertIn("no shipping quote", got["102"]["flags"])  # declined quote is never clean
        self.assertLess(got["202"]["headroom"], 0)
        self.assertNotIn("202", {r["item_id"] for r in app.rows()})


if __name__ == "__main__":
    unittest.main()
