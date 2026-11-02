#!/usr/bin/env python3
"""sgw-watch (Resell-Scanner): ShopGoodwill watch list for roger. Stdlib only.

  python3 app.py serve [--host 0.0.0.0] [--port 8088]   web app
  python3 app.py scan                                   one scan pass (the timer runs this)
  python3 app.py export                                 categories and models as JSON on stdout

Categories group watches (Cameras, iPods, Games...). A watch is a set of
search keywords, how a title has to match them, words that rule a listing out,
and the most you'll pay landed (bid + shipping + handling). All of it is
edited in the web app and lives in SQLite next to this file. Nothing here
talks to Marginspace or Neon.
"""

from __future__ import annotations

import argparse
import fcntl
import html
import json
import os
import re
import sqlite3
import sys
import threading
import traceback
from contextlib import contextmanager
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import sgw

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SGW_DB", os.path.join(HERE, "sgw-watch.db"))
LOCK_PATH = DB_PATH + ".scan.lock"
TIERS = ("winner", "new", "try")

# How a listing's title has to relate to a watch's keywords.
MATCH_MODES = {
    "model": "Model number: title has the last word of a keyword (SX110), digit-bounded",
    "all": "All words: title has every word of at least one keyword",
    "search": "Anything the search returns",
}

# From the Camera Sourcing Cheat Sheet (Peter's 28 Marginspace camera sales,
# Aug-Oct 2026). Seeded once into an empty database; after that the web app
# is the source of truth.
SEED = [
    ("Canon SX100/110/120 IS", ["Canon SX100", "Canon SX110", "Canon SX120"], 45, "winner", "Only compacts allowed up to $45 with Fuji F30/F31fd."),
    ("Sony Cyber-shot W", ["Sony DSC-W7", "Sony DSC-W50", "Sony DSC-W55", "Sony DSC-W80", "Sony DSC-W120", "Sony DSC-W300"], 35, "winner", ""),
    ("Nikon Coolpix L18/S", ["Coolpix L18", "Coolpix S4000", "Coolpix S200", "Coolpix S210", "Coolpix S500"], 25, "winner", ""),
    ("Canon ELPH / A-series", ["Canon SD200", "Canon SD770", "Canon SD1000", "Canon SD500", "Canon A510", "Canon A1000", "Powershot S200"], 35, "winner", ""),
    ("Sony Cyber-shot S/P", ["Sony DSC-S30", "Sony DSC-P9", "Sony DSC-S50"], 18, "winner", ""),
    ("Panasonic Lumix TZ/FX", ["Lumix TZ5", "Lumix FX35"], 15, "winner", ""),
    ("Olympus FE", ["Olympus FE-20", "Olympus FE-280"], 25, "winner", ""),
    ("Kodak Zi8", ["Kodak Zi8"], 12, "winner", "The one pocket video camera worth buying."),
    ("Sony Cyber-shot T", ["Sony DSC-T9", "Sony DSC-T10", "Sony DSC-T20"], 35, "new", "Unproven: check eBay sold comps first."),
    ("Nikon Coolpix 2500", ["Coolpix 2500"], 20, "new", "Unproven: check eBay sold comps first."),
    ("Fujifilm F30/F31fd", ["Finepix F30", "Finepix F31fd"], 45, "new", "Unproven: check eBay sold comps first."),
    ("Fujifilm F20/F40fd", ["Finepix F20", "Finepix F40fd"], 25, "try", "Buy at most one to test."),
    ("Panasonic LX3", ["Lumix LX3"], 40, "try", "Buy at most one to test."),
]

DEFAULTS = {
    "zipcode": "78602",
    "state": "TX",
    "ship_estimate": "12",
    "max_quotes": "60",
    "timezone": "America/Chicago",
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "red_flags": "parts, repair, as is, as-is, not working, broken, lens error, untested",
    "skip_words": "case only, battery only, charger only, manual only, lens cap, box only",
}


# ---------------------------------------------------------------- database

@contextmanager
def db():
    """One connection per use: commits on success, rolls back on error, always closes."""
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def _columns(con, table):
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}


def init():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL UNIQUE COLLATE NOCASE,
          notes TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS models (
          id INTEGER PRIMARY KEY,
          label TEXT NOT NULL,
          terms TEXT NOT NULL,            -- search keywords, one per line
          match TEXT NOT NULL DEFAULT '', -- optional title tokens, comma separated; overrides match_mode
          max_buy REAL NOT NULL,          -- most you'll pay landed (bid + shipping + handling)
          tier TEXT NOT NULL DEFAULT 'winner',
          notes TEXT NOT NULL DEFAULT '',
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS listings (
          item_id TEXT PRIMARY KEY,
          model_id INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
          term TEXT, title TEXT, price REAL, bids INTEGER, is_auction INTEGER,
          end_utc TEXT, image TEXT, seller TEXT,
          shipping REAL, handling REAL,   -- NULL = not quoted; never read as 0
          ship_status TEXT NOT NULL DEFAULT 'pending',  -- pending | quoted | none
          flags TEXT NOT NULL DEFAULT '',
          first_seen TEXT, last_seen TEXT,
          alerted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS scans (
          id INTEGER PRIMARY KEY,
          started TEXT, finished TEXT, trigger TEXT,
          status TEXT,             -- running | ok | partial | failed
          progress TEXT NOT NULL DEFAULT '',
          matches INTEGER NOT NULL DEFAULT 0,
          quotes INTEGER NOT NULL DEFAULT 0,
          errors TEXT NOT NULL DEFAULT ''
        );
        """)
        # Upgrade a database made by the first version (no categories).
        cols = _columns(con, "models")
        if "category_id" not in cols:
            con.execute("ALTER TABLE models ADD COLUMN category_id INTEGER REFERENCES categories(id)")
        if "match_mode" not in cols:
            con.execute("ALTER TABLE models ADD COLUMN match_mode TEXT NOT NULL DEFAULT 'model'")
        if "exclude" not in cols:
            con.execute("ALTER TABLE models ADD COLUMN exclude TEXT NOT NULL DEFAULT ''")

        fresh = con.execute("SELECT COUNT(*) FROM settings WHERE key='seeded'").fetchone()[0] == 0
        if fresh and con.execute("SELECT COUNT(*) FROM models").fetchone()[0] == 0:
            cam = con.execute("INSERT INTO categories(name) VALUES ('Cameras')").lastrowid
            for label, terms, mx, tier, notes in SEED:
                con.execute("INSERT INTO models(label, terms, max_buy, tier, notes, category_id) VALUES (?,?,?,?,?,?)",
                            (label, "\n".join(terms), mx, tier, notes, cam))
        if fresh:
            con.execute("INSERT INTO settings VALUES ('seeded', ?)", (now_iso(),))
        # Watches from before categories existed were all cameras.
        if con.execute("SELECT COUNT(*) FROM models WHERE category_id IS NULL").fetchone()[0]:
            con.execute("INSERT OR IGNORE INTO categories(name) VALUES ('Cameras')")
            cam = con.execute("SELECT id FROM categories WHERE name='Cameras'").fetchone()[0]
            con.execute("UPDATE models SET category_id=? WHERE category_id IS NULL", (cam,))
        # A scan killed mid-pass (reboot, timeout) must not read as running forever.
        con.execute("UPDATE scans SET status='failed', errors=errors || 'interrupted', finished=? "
                    "WHERE status='running' AND started < ?",
                    (now_iso(), (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")))


def settings() -> dict:
    with db() as con:
        s = dict(DEFAULTS)
        s.update({r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")})
        return s


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def words(csv_text: str) -> list[str]:
    return [w.strip().lower() for w in (csv_text or "").replace("\n", ",").split(",") if w.strip()]


def term_list(text: str) -> list[str]:
    seen, out = set(), []
    for t in (text or "").splitlines():
        t = " ".join(t.split())
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


# ---------------------------------------------------------------- matching

def tokens_for(model) -> list[str]:
    """Title tokens for match_mode 'model' (or a custom `match` list).

    The last word of each keyword, with Sony's DSC- prefix dropped
    ("Sony DSC-W7" -> "w7"), since listers write W7, DSC-W7 and DSCW7 alike.
    """
    if (model.get("match") or "").strip():
        return words(model["match"])
    out = []
    for t in term_list(model["terms"]):
        out.append(re.sub(r"^dsc-?", "", t.split()[-1].lower()))
    return out


def title_matches(title: str, toks: list[str]) -> bool:
    """Digit-bounded, so W7 doesn't catch W70 and F30 doesn't catch F300."""
    t = title.lower()
    return any(re.search(rf"(?<!\d){re.escape(tok)}(?!\d)", t) for tok in toks)


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower())


def matches(model, title: str) -> bool:
    """Does this title count as this watch? Excludes first, then the match rule."""
    t = title.lower()
    if any(re.search(rf"\b{re.escape(w)}\b", t) for w in words(model.get("exclude", ""))):
        return False
    mode = model.get("match_mode") or "model"
    if (model.get("match") or "").strip() or mode == "model":
        return title_matches(title, tokens_for(model))
    if mode == "all":
        nt = f" {_norm(title)} "
        return any(all(f" {w} " in nt for w in _norm(term).split()) for term in term_list(model["terms"]))
    return True  # "search": trust ShopGoodwill's own match


def flags_for(title: str, red: list[str]) -> list[str]:
    t = title.lower()
    out = [w for w in red if re.search(rf"\b{re.escape(w)}\b", t)]
    if re.search(r"\blot\b|\blot of\b|\bbundle of\b|\(\d+\)\s*camera", t):
        out.append("lot")
    return out


# ---------------------------------------------------------------- scan

def run_scan(trigger="manual") -> str:
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return "A scan is already running."
    try:
        return _scan(trigger)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _scan(trigger):
    s = settings()
    red, skip = words(s["red_flags"]), words(s["skip_words"])
    with db() as con:
        models = [dict(r) for r in con.execute("SELECT * FROM models WHERE enabled=1 ORDER BY id")]
        scan_id = con.execute("INSERT INTO scans(started, trigger, status) VALUES (?,?, 'running')",
                              (now_iso(), trigger)).lastrowid
    # One request per distinct keyword, even when two watches share one.
    by_term: dict[str, list[dict]] = {}
    for m in models:
        for t in term_list(m["terms"]):
            by_term.setdefault(t.lower(), []).append(m)
    jobs = list(by_term.items())
    errors, matched, seen = [], 0, set()

    for n, (term, ms) in enumerate(jobs, 1):
        _progress(scan_id, f"Searching {n}/{len(jobs)}: {term}")
        try:
            results = sgw.search(term)
        except sgw.ShapeError as e:
            errors.append(f"{term}: {e}")
            break  # the API changed; stop rather than hammer it with bad requests
        except Exception as e:
            errors.append(f"{term}: {type(e).__name__}: {e}")
            if len(errors) >= 3 and matched == 0 and n <= 3:
                errors.append("Stopped: the first searches all failed (network or block).")
                break
            continue
        with db() as con:
            for li in results:
                t = li["title"].lower()
                if li["item_id"] in seen or any(w in t for w in skip):
                    continue
                m = next((m for m in ms if matches(m, li["title"])), None)
                if not m:
                    continue
                seen.add(li["item_id"])
                matched += 1
                con.execute("""
                  INSERT INTO listings(item_id, model_id, term, title, price, bids, is_auction,
                                       end_utc, image, seller, flags, first_seen, last_seen)
                  VALUES (:item_id, :model_id, :term, :title, :price, :bids, :is_auction,
                          :end_utc, :image, :seller, :flags, :now, :now)
                  ON CONFLICT(item_id) DO UPDATE SET
                    model_id=excluded.model_id, term=excluded.term, title=excluded.title,
                    price=excluded.price, bids=excluded.bids, end_utc=excluded.end_utc,
                    image=excluded.image, flags=excluded.flags, last_seen=excluded.last_seen
                """, dict(li, model_id=m["id"], term=term, flags=", ".join(flags_for(li["title"], red)),
                          is_auction=int(li["is_auction"]), now=now_iso()))

    # Real shipping, only for listings that could still come in under max buy
    # on the bid alone, and only once per item (shipping doesn't change).
    quotes = 0
    if not any("Stopped" in e or "shape" in e for e in errors):
        with db() as con:
            todo = con.execute("""
              SELECT l.item_id FROM listings l JOIN models m ON m.id = l.model_id
              WHERE l.ship_status = 'pending' AND l.price <= m.max_buy AND l.end_utc > ?
              ORDER BY l.end_utc LIMIT ?""", (now_iso(), int(float(s["max_quotes"])))).fetchall()
        for n, r in enumerate(todo, 1):
            _progress(scan_id, f"Shipping quotes {n}/{len(todo)}")
            try:
                q = sgw.shipping_quote(r["item_id"], s["zipcode"], s["state"])
            except sgw.ShapeError as e:
                errors.append(f"shipping: {e}")
                break
            quotes += 1
            with db() as con:
                if q:
                    con.execute("UPDATE listings SET shipping=?, handling=?, ship_status='quoted' "
                                "WHERE item_id=?", (q[0], q[1], r["item_id"]))
                else:
                    con.execute("UPDATE listings SET ship_status='none' WHERE item_id=?", (r["item_id"],))

    with db() as con:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        con.execute("DELETE FROM listings WHERE end_utc < ?", (cutoff,))
        status = "failed" if errors and matched == 0 else ("partial" if errors else "ok")
        con.execute("UPDATE scans SET finished=?, status=?, progress='', matches=?, quotes=?, errors=? "
                    "WHERE id=?", (now_iso(), status, matched, quotes, "\n".join(errors), scan_id))
        con.execute("DELETE FROM scans WHERE id < ? - 200", (scan_id,))
    _push_new(s)
    return f"Scan {status}: {matched} matches, {quotes} shipping quotes, {len(errors)} errors."


def _progress(scan_id, text):
    with db() as con:
        con.execute("UPDATE scans SET progress=? WHERE id=?", (text, scan_id))


# ---------------------------------------------------------------- results

ENDING = {"": "Any time", "1": "Next hour", "6": "Next 6 hours", "24": "Next 24 hours", "72": "Next 3 days"}
SORTS = {"end": "Ending soonest", "room": "Most room", "landed": "Cheapest landed", "new": "Newest found"}


def rows(f: dict | None = None):
    """Live listings with landed cost worked out, filtered.

    f keys (all optional, as strings from the query): cat, model, tier, q,
    max (landed $), ending (hours), over=1 (include over max buy),
    flagged=0 (hide flagged), buynow=0 (hide buy-now), sort.
    """
    f = f or {}
    s = settings()
    est = float(s["ship_estimate"])
    q = """SELECT l.*, m.label, m.tier, m.max_buy, m.category_id, c.name AS category
           FROM listings l JOIN models m ON m.id = l.model_id
           LEFT JOIN categories c ON c.id = m.category_id WHERE l.end_utc > ?"""
    args: list = [now_iso()]
    if f.get("cat", "").isdigit():
        q += " AND m.category_id = ?"
        args.append(int(f["cat"]))
    if f.get("model", "").isdigit():
        q += " AND m.id = ?"
        args.append(int(f["model"]))
    if f.get("tier") in TIERS:
        q += " AND m.tier = ?"
        args.append(f["tier"])
    if f.get("ending", "").isdigit():
        q += " AND l.end_utc <= ?"
        args.append((datetime.now(timezone.utc) + timedelta(hours=int(f["ending"]))).isoformat(timespec="seconds"))
    if f.get("buynow") == "0":
        q += " AND l.is_auction = 1"
    needle = (f.get("q") or "").strip().lower()
    try:
        cap = float(f["max"]) if f.get("max") else None
    except ValueError:
        cap = None

    out = []
    with db() as con:
        for r in con.execute(q + " ORDER BY l.end_utc", args):
            r = dict(r)
            if needle and needle not in r["title"].lower():
                continue
            if r["ship_status"] == "quoted":
                ship = (r["shipping"] or 0) + (r["handling"] or 0)
                r["ship_known"] = True
            else:
                ship = est
                r["ship_known"] = False
            if r["ship_status"] == "none":
                # The calculator declined (often pickup only). Unknown, not free:
                # never let it read as clean.
                r["flags"] = ", ".join(x for x in (r["flags"], "no shipping quote") if x)
            r["ship_total"] = ship
            r["landed"] = r["price"] + ship
            r["headroom"] = r["max_buy"] - r["landed"]
            r["bid_up_to"] = r["max_buy"] - ship
            if r["headroom"] < 0 and f.get("over") != "1":
                continue
            if r["flags"] and f.get("flagged") == "0":
                continue
            if cap is not None and r["landed"] > cap:
                continue
            out.append(r)
    sort = f.get("sort", "end")
    if sort == "room":
        out.sort(key=lambda r: -r["headroom"])
    elif sort == "landed":
        out.sort(key=lambda r: r["landed"])
    elif sort == "new":
        out.sort(key=lambda r: r["first_seen"] or "", reverse=True)
    return out


def _push_new(s):
    """ntfy for new, clean, shipping-quoted listings under max buy. Optional."""
    if not s["ntfy_topic"].strip():
        return
    for r in rows():
        if r["alerted"] or r["flags"] or not r["ship_known"]:
            continue
        body = (f"Bid ${r['price']:.2f} + ship ${r['ship_total']:.2f} = ${r['landed']:.2f} "
                f"(max ${r['max_buy']:.0f}). Bid up to ${r['bid_up_to']:.2f}. Ends {fmt_end(r['end_utc'], s)}.")
        try:
            req = urllib.request.Request(
                f"{s['ntfy_server'].rstrip('/')}/{s['ntfy_topic'].strip()}", data=body.encode(),
                headers={"Title": f"{r['label']}: {r['title'][:80]}".encode("utf-8").decode("latin-1", "ignore"),
                         "Click": f"https://shopgoodwill.com/item/{r['item_id']}", "Tags": "mag"})
            urllib.request.urlopen(req, timeout=15).close()
        except Exception as e:
            print(f"ntfy failed: {e}", file=sys.stderr)
            return
        with db() as con:
            con.execute("UPDATE listings SET alerted=1 WHERE item_id=?", (r["item_id"],))


def fmt_end(end_utc, s):
    if not end_utc:
        return "?"
    dt = datetime.fromisoformat(end_utc)
    local = dt.astimezone(ZoneInfo(s["timezone"]))
    secs = int((dt - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        rel = "ended"
    elif secs < 3600:
        rel = f"{secs // 60}m"
    elif secs < 86400:
        rel = f"{secs // 3600}h {secs % 3600 // 60}m"
    else:
        rel = f"{secs // 86400}d {secs % 86400 // 3600}h"
    return f"{local:%a %-I:%M %p} ({rel})"


def fmt_ago(iso, s):
    if not iso:
        return "never"
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(s["timezone"])).strftime("%a %b %-d, %-I:%M %p")


# ---------------------------------------------------------------- web

CSS = """
:root{--bg:#f6f6f4;--fg:#1d1d1b;--mut:#6b6b66;--card:#fff;--line:#e4e4df;--acc:#0b57d0;--ok:#137333;--bad:#b3261e;--warn:#8a5a00;--chip:#efefea}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ececea;--mut:#9a9a94;--card:#1d1d1b;--line:#2e2e2b;--acc:#8ab4f8;--ok:#6dd58c;--bad:#f2b8b5;--warn:#f0c674;--chip:#2a2a27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,sans-serif}
header{display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--card)}
header b{font-size:16px}nav a{margin-right:14px;color:var(--fg);text-decoration:none}nav a.on{color:var(--acc);font-weight:600}
main{padding:16px;max-width:1200px;margin:0 auto}a{color:var(--acc)}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 10px}.mut{color:var(--mut)}small{color:var(--mut)}
button,.btn{font:inherit;padding:7px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;text-decoration:none;display:inline-block}
button.pri{background:var(--acc);border-color:var(--acc);color:#fff}button.del{color:var(--bad)}button.sm,.btn.sm{padding:3px 9px;font-size:13px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pos{color:var(--ok);font-weight:600}.neg{color:var(--bad)}.flag{color:var(--bad);font-size:13px}.est{color:var(--warn)}
.chip{display:inline-flex;align-items:center;gap:2px;padding:1px 7px;border-radius:99px;background:var(--chip);font-size:12px;margin:2px 3px 2px 0}
.chip button{border:0;background:none;padding:0 0 0 3px;color:var(--mut);font-size:14px;line-height:1}
img.th{width:56px;height:56px;object-fit:cover;border-radius:6px;background:var(--chip)}
form.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;display:grid;gap:10px;max-width:680px}
label{display:grid;gap:4px;font-size:13px;color:var(--mut)}input,textarea,select{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg)}
.bar input,.bar select{padding:6px 8px}textarea{min-height:110px}.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}.msg{padding:10px 12px;border-radius:8px;background:var(--chip);margin-bottom:12px}
.err{color:var(--bad)}h2{font-size:15px;margin:22px 0 8px}h2 small{font-weight:400}.off td{opacity:.5}.inline{display:inline}
.scroll{overflow-x:auto;border-radius:10px}.show-sm{display:none}.addterm{display:flex;gap:4px;margin-top:4px}.addterm input{padding:3px 7px;font-size:13px;width:160px}
form.card input,form.card select,form.card textarea{width:100%;min-width:0}form.card label,.row>*{min-width:0}
.check{display:flex;gap:5px;align-items:center;font-size:14px;color:var(--fg)}
@media (max-width:720px){.hide-sm{display:none}.show-sm{display:block}td,th{padding:7px 5px;font-size:13px}.row{grid-template-columns:1fr}
header{gap:8px}header .mut{margin-left:0!important;width:100%;font-size:13px}td small{font-size:11px}}
"""


def e(v):
    return html.escape(str(v if v is not None else ""), quote=True)


def categories():
    with db() as con:
        return [dict(r) for r in con.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM models m WHERE m.category_id=c.id) AS n FROM categories c ORDER BY c.name")]


def page(title, body, active="", refresh=None):
    s = settings()
    with db() as con:
        last = con.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    if last and last["status"] == "running":
        status = f"<span class='est'>Scanning… {e(last['progress'])}</span>"
        refresh = refresh or 5
    elif last:
        cls = "pos" if last["status"] == "ok" else "err"
        status = (f"Last scan {e(fmt_ago(last['finished'], s))}: <span class='{cls}'>{e(last['status'])}</span>, "
                  f"{last['matches']} matches")
    else:
        status = "No scans yet."
    nav = "".join(f"<a href='{h}' class='{'on' if a == active else ''}'>{t}</a>"
                  for h, t, a in (("/", "Results", "results"), ("/models", "Watches", "models"),
                                  ("/categories", "Categories", "categories"),
                                  ("/scans", "Scan log", "scans"), ("/settings", "Settings", "settings")))
    meta = f"<meta http-equiv='refresh' content='{refresh}'>" if refresh else ""
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>{meta}
<title>{e(title)} · SGW watch</title><style>{CSS}</style></head><body>
<header><b>SGW watch</b><nav>{nav}</nav><span class='mut' style='margin-left:auto'>{status}</span>
<form method=post action=/scan class=inline><button class=pri>Scan now</button></form></header>
<main>{body}</main></body></html>"""


def _select(name, options, current, blank=None):
    opts = (f"<option value=''>{e(blank)}</option>" if blank is not None else "") + "".join(
        f"<option value='{e(v)}' {'selected' if str(v) == str(current) else ''}>{e(t)}</option>" for v, t in options)
    return f"<select name={name} onchange='this.form.submit()'>{opts}</select>"


def results_page(f):
    s = settings()
    f.setdefault("flagged", "1")
    rs = rows(f)
    cats = categories()
    with db() as con:
        sql = "SELECT id, label FROM models"
        args = ()
        if f.get("cat", "").isdigit():
            sql += " WHERE category_id=?"
            args = (int(f["cat"]),)
        models = con.execute(sql + " ORDER BY label", args).fetchall()

    def chk(name, on_value, label, checked):
        return (f"<label class=check><input type=checkbox name={name} value={on_value} "
                f"{'checked' if checked else ''} onchange='this.form.submit()'> {label}</label>")

    # flagged is on by default; an unchecked box sends nothing, so carry an explicit 0.
    bar = f"""<form class=bar method=get>
{_select('cat', [(c['id'], c['name']) for c in cats], f.get('cat', ''), 'All categories')}
{_select('model', [(m['id'], m['label']) for m in models], f.get('model', ''), 'All watches')}
{_select('tier', [(t, t) for t in TIERS], f.get('tier', ''), 'Any tier')}
{_select('ending', [(k, v) for k, v in ENDING.items() if k], f.get('ending', ''), 'Ending any time')}
{_select('sort', list(SORTS.items()), f.get('sort', 'end'))}
<input name=q value='{e(f.get('q', ''))}' placeholder='Title contains' size=14>
<input name=max value='{e(f.get('max', ''))}' placeholder='Max landed $' size=9 inputmode=decimal>
{''.join(f"<input type=hidden name={k} value='{e(f[k])}'>" for k in ('over', 'flagged', 'buynow') if k in f)}
<button>Filter</button><a class=btn href='/'>Reset</a></form>
<form class=bar method=get>{''.join(f"<input type=hidden name={k} value='{e(v)}'>" for k, v in f.items() if k not in ('over', 'flagged', 'buynow'))}
{chk('over', '1', 'Show over max', f.get('over') == '1')}
<input type=hidden name=flagged value=0>{chk('flagged', '1', 'Show flagged', f.get('flagged') != '0')}
<input type=hidden name=buynow value=0>{chk('buynow', '1', 'Show buy now', f.get('buynow') != '0')}
</form>"""

    def table(items, empty):
        if not items:
            return f"<p class=mut>{empty}</p>"
        trs = []
        for r in items:
            ship = (f"${r['ship_total']:.2f}" if r["ship_known"]
                    else f"<span class=est title='Estimate: no real quote'>~${r['ship_total']:.0f}</span>")
            room = f"<span class='{'pos' if r['headroom'] >= 0 else 'neg'}'>{'-' if r['headroom'] < 0 else ''}${abs(r['headroom']):.0f}</span>"
            upto = f"${r['bid_up_to']:.2f}" if r["bid_up_to"] > r["price"] else "<span class=neg>—</span>"
            fl = f"<div class=flag>{e(r['flags'])}</div>" if r["flags"] else ""
            img = f"<img class=th src='{e(r['image'])}' loading=lazy alt=''>" if r["image"] else ""
            kind = "" if r["is_auction"] else " <span class=chip>buy now</span>"
            trs.append(f"""<tr><td class=hide-sm>{img}</td>
<td><a href='https://shopgoodwill.com/item/{e(r['item_id'])}' target=_blank rel=noopener>{e(r['title'])}</a>{kind}{fl}
<div><small>{e(r['category'] or '')} › {e(r['label'])} · <span class=chip>{e(r['tier'])}</span></small></div>
<div class=show-sm><small>Ends {e(fmt_end(r['end_utc'], s))}</small></div></td>
<td class=n>${r['price']:.2f}<br><small>{r['bids'] or 0} bids</small></td><td class='n hide-sm'>{ship}</td>
<td class=n>${r['landed']:.2f}</td><td class='n hide-sm'>${r['max_buy']:.0f}</td><td class=n>{room}</td>
<td class=n>{upto}</td><td class='n hide-sm'><small>{e(fmt_end(r['end_utc'], s))}</small></td></tr>""")
        return ("<div class=scroll><table><tr><th class=hide-sm></th><th>Listing</th><th class=n>Bid</th>"
                "<th class='n hide-sm'>Ship</th><th class=n>Landed</th><th class='n hide-sm'>Max</th>"
                "<th class=n>Room</th><th class=n>Bid up to</th><th class='n hide-sm'>Ends</th></tr>"
                + "".join(trs) + "</table></div>")

    clean = [r for r in rs if not r["flags"]]
    flagged = [r for r in rs if r["flags"]]
    body = bar + f"""<p class=mut>Landed = bid + shipping + handling. Check photos (battery, screen, lens, haze) before bidding.</p>
<h2>Clean ({len(clean)})</h2>{table(clean, "Nothing matches these filters right now.")}"""
    if f.get("flagged") != "0":
        body += f"<h2>Flagged: parts / repair / lot / untested ({len(flagged)})</h2>{table(flagged, 'None.')}"
    return page("Results", body, "results")


def models_page(f, msg="", form=None, err=""):
    cats = categories()
    where, args = ["1=1"], [now_iso()]
    if f.get("cat", "").isdigit():
        where.append("m.category_id = ?")
        args.append(int(f["cat"]))
    needle = (f.get("q") or "").strip().lower()
    with db() as con:
        ms = [dict(r) for r in con.execute(f"""
            SELECT m.*, c.name AS category,
              (SELECT COUNT(*) FROM listings l WHERE l.model_id=m.id AND l.end_utc > ?) AS live
            FROM models m LEFT JOIN categories c ON c.id = m.category_id
            WHERE {' AND '.join(where)}
            ORDER BY c.name, m.tier='try', m.tier='new', m.label""", args)]
    if needle:
        ms = [m for m in ms if needle in (m["label"] + "\n" + m["terms"] + "\n" + m["notes"]).lower()]

    groups: dict[str, list] = {}
    for m in ms:
        groups.setdefault(m["category"] or "Uncategorized", []).append(m)

    def chips(m):
        out = []
        for t in term_list(m["terms"]):
            out.append(f"""<span class=chip>{e(t)}<form method=post action='/models/{m['id']}/term-remove' class=inline>
<input type=hidden name=term value='{e(t)}'><button title='Remove keyword' aria-label='Remove {e(t)}'>×</button></form></span>""")
        return "".join(out)

    def row(m):
        rule = "custom: " + m["match"] if m["match"].strip() else {"model": "model number", "all": "all words",
                                                                     "search": "any search hit"}.get(m["match_mode"], "")
        extra = f" · excludes: {e(m['exclude'])}" if m["exclude"].strip() else ""
        return f"""<tr class='{'' if m['enabled'] else 'off'}'>
<td><b>{e(m['label'])}</b><div>{chips(m)}</div>
<form method=post action='/models/{m['id']}/term-add' class=addterm><input name=term placeholder='Add keyword' aria-label='Add keyword to {e(m['label'])}'><button class=sm>Add</button></form>
<div><small>Match: {e(rule)}{extra}</small></div>
{f"<div><small>{e(m['notes'])}</small></div>" if m['notes'] else ''}</td>
<td><span class=chip>{e(m['tier'])}</span></td><td class=n>${m['max_buy']:.0f}</td>
<td class=n><a href='/?model={m['id']}&over=1'>{m['live']}</a></td>
<td class=n style='white-space:nowrap'>
<form method=post action='/models/{m['id']}/toggle' class=inline><button class=sm>{'Pause' if m['enabled'] else 'Resume'}</button></form>
<a class='btn sm' href='/models/{m['id']}'>Edit</a>
<form method=post action='/models/{m['id']}/delete' class=inline onsubmit="return confirm('Delete {e(m['label'])}?')"><button class='sm del'>Delete</button></form>
</td></tr>"""

    tables = "".join(
        f"<h2>{e(name)} <small>({len(items)})</small></h2><div class=scroll><table><tr><th>Watch</th><th>Tier</th>"
        f"<th class=n>Max landed</th><th class=n>Live</th><th></th></tr>{''.join(map(row, items))}</table></div>"
        for name, items in groups.items()) or "<p class=mut>No watches match.</p>"
    bar = f"""<form class=bar method=get>{_select('cat', [(c['id'], c['name']) for c in cats], f.get('cat', ''), 'All categories')}
<input name=q value='{e(f.get('q', ''))}' placeholder='Find a watch or keyword' size=22><button>Find</button>
<a class=btn href='#add'>+ New watch</a> <a class=btn href='/categories'>Categories</a></form>"""
    body = (f"<div class=msg>{e(msg)}</div>" if msg else "") + bar + tables + \
        "<h2 id=add>New watch</h2>" + model_form(form or {"category_id": f.get("cat", "")}, "/models", "Add watch", err) + \
        "<p class=mut>Export: <a href='/export.json'>export.json</a></p>"
    return page("Watches", body, "models")


def model_form(f, action, label, err=""):
    cats = categories()
    tier = f.get("tier", "winner")
    mode = f.get("match_mode") or "model"
    cat_opts = "".join(f"<option value='{c['id']}' {'selected' if str(c['id']) == str(f.get('category_id', '')) else ''}>"
                       f"{e(c['name'])}</option>" for c in cats)
    tier_opts = "".join(f"<option {'selected' if t == tier else ''}>{t}</option>" for t in TIERS)
    mode_opts = "".join(f"<option value='{k}' {'selected' if k == mode else ''}>{e(v)}</option>" for k, v in MATCH_MODES.items())
    return f"""<form class=card method=post action='{action}'>{f"<div class=err>{e(err)}</div>" if err else ''}
<div class=row><label>Name<input name=label required value='{e(f.get('label', ''))}' placeholder='iPod Classic 6th/7th gen'></label>
<label>Category<select name=category_id>{cat_opts}<option value=new>+ New category…</option></select></label></div>
<label class=newcat>New category name (only if you picked "+ New category")<input name=new_category value='{e(f.get('new_category', ''))}'></label>
<label>Search keywords, one per line (each is one ShopGoodwill search)<textarea name=terms required placeholder='iPod Classic 160GB&#10;iPod Classic 120GB'>{e(f.get('terms', ''))}</textarea></label>
<label>How a title has to match<select name=match_mode>{mode_opts}</select></label>
<label>Or: title must contain one of these (comma separated, overrides the above)<input name=match value='{e(f.get('match', ''))}' placeholder='F31fd, F31 fd'></label>
<label>Exclude listings with any of these words (comma separated)<input name=exclude value='{e(f.get('exclude', ''))}' placeholder='nano, shuffle, case'></label>
<div class=row><label>Max landed buy ($)<input name=max_buy type=number step=0.01 min=1 required value='{e(f.get('max_buy', ''))}'></label>
<label>Tier<select name=tier>{tier_opts}</select></label></div>
<label>Notes<input name=notes value='{e(f.get('notes', ''))}'></label>
<div><button class=pri>{label}</button></div></form>
<script>document.querySelectorAll('select[name=category_id]').forEach(function(s){{
var l=s.form.querySelector('.newcat');function u(){{l.style.display=s.value==='new'?'':'none'}};s.onchange=u;u()}})</script>"""


def categories_page(msg="", err=""):
    cats = categories()
    trs = "".join(f"""<tr><td><form method=post action='/categories/{c['id']}' class=bar style='margin:0'>
<input name=name value='{e(c['name'])}' required aria-label='Category name'><input name=notes value='{e(c['notes'])}' placeholder='Notes' size=30>
<button class=sm>Save</button></form></td>
<td class=n><a href='/models?cat={c['id']}'>{c['n']} watches</a></td>
<td class=n><a class='btn sm' href='/?cat={c['id']}'>Results</a>
<form method=post action='/categories/{c['id']}/delete' class=inline onsubmit="return confirm('Delete {e(c['name'])}?')"><button class='sm del'>Delete</button></form></td></tr>"""
                  for c in cats)
    body = (f"<div class=msg>{e(msg)}</div>" if msg else "") + (f"<div class='msg err'>{e(err)}</div>" if err else "") + \
        f"<div class=scroll><table><tr><th>Category</th><th class=n>Watches</th><th></th></tr>{trs}</table></div>" + \
        """<h2>New category</h2><form class=card method=post action=/categories>
<div class=row><label>Name<input name=name required placeholder='iPods'></label><label>Notes<input name=notes></label></div>
<div><button class=pri>Add category</button></div></form>"""
    return page("Categories", body, "categories")


def scans_page():
    s = settings()
    with db() as con:
        sc = con.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 50").fetchall()
    trs = "".join(f"""<tr><td>{e(fmt_ago(x['started'], s))}</td><td>{e(x['trigger'])}</td>
<td class='{'pos' if x['status'] == 'ok' else 'err'}'>{e(x['status'])}</td><td class=n>{x['matches']}</td>
<td class=n>{x['quotes']}</td><td><small style='white-space:pre-wrap'>{e(x['errors'] or x['progress'])}</small></td></tr>"""
                  for x in sc)
    return page("Scan log", "<div class=scroll><table><tr><th>Started</th><th>By</th><th>Status</th><th class=n>Matches</th>"
                "<th class=n>Quotes</th><th>Errors</th></tr>" + trs + "</table></div>", "scans")


def settings_page(msg=""):
    s = settings()
    f = lambda k, lab, t="text": f"<label>{lab}<input name={k} type={t} value='{e(s[k])}'></label>"
    body = (f"<div class=msg>{e(msg)}</div>" if msg else "") + f"""<form class=card method=post action=/settings>
<div class=row>{f('zipcode', 'Ship-to ZIP')}{f('state', 'State')}</div>
<div class=row>{f('ship_estimate', 'Shipping estimate until quoted ($)', 'number')}{f('max_quotes', 'Max shipping quotes per scan', 'number')}</div>
{f('timezone', 'Time zone')}
<div class=row>{f('ntfy_server', 'ntfy server')}{f('ntfy_topic', 'ntfy topic (blank = no push)')}</div>
<label>Red flags, every watch (comma separated; flagged, still shown)<textarea name=red_flags>{e(s['red_flags'])}</textarea></label>
<label>Skip words, every watch (comma separated; dropped)<textarea name=skip_words>{e(s['skip_words'])}</textarea></label>
<div><button class=pri>Save</button></div></form>"""
    return page("Settings", body, "settings")


def parse_model(con, f):
    """Validate a watch form. Returns (data, error). Creates a new category if asked."""
    data = {"label": f.get("label", "").strip(), "terms": "\n".join(term_list(f.get("terms", ""))),
            "match": f.get("match", "").strip(), "exclude": f.get("exclude", "").strip(),
            "match_mode": f.get("match_mode") if f.get("match_mode") in MATCH_MODES else "model",
            "tier": f.get("tier") if f.get("tier") in TIERS else "winner", "notes": f.get("notes", "").strip()}
    try:
        data["max_buy"] = round(float(f.get("max_buy", "")), 2)
    except ValueError:
        return data, "Max landed buy must be a number."
    if not data["label"]:
        return data, "Name is required."
    if not data["terms"]:
        return data, "Add at least one search keyword."
    if data["max_buy"] <= 0:
        return data, "Max landed buy must be more than $0."
    cat = f.get("category_id", "")
    if cat == "new":
        name = f.get("new_category", "").strip()
        if not name:
            return data, "Type a name for the new category."
        row = con.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
        data["category_id"] = row["id"] if row else con.execute(
            "INSERT INTO categories(name) VALUES (?)", (name,)).lastrowid
    elif cat.isdigit() and con.execute("SELECT 1 FROM categories WHERE id=?", (int(cat),)).fetchone():
        data["category_id"] = int(cat)
    else:
        return data, "Pick a category."
    return data, ""


def refile(con, model_id):
    """After a watch changes, drop its stored listings that no longer match it."""
    m = dict(con.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone())
    for l in con.execute("SELECT item_id, title FROM listings WHERE model_id=?", (model_id,)).fetchall():
        if not matches(m, l["title"]):
            con.execute("DELETE FROM listings WHERE item_id=?", (l["item_id"],))


def back(path, msg):
    return path + ("&" if "?" in path else "?") + "msg=" + urllib.parse.quote(msg)


class Handler(BaseHTTPRequestHandler):
    server_version = "sgw-watch"

    def log_message(self, fmt, *args):
        pass

    def send(self, body, code=200, ctype="text/html; charset=utf-8"):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def redirect(self, to):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def fault(self):
        traceback.print_exc()
        self.send(page("Error", "<p class=err>Something broke. The traceback is in the service log "
                       "(journalctl -u sgw-watch).</p>"), 500)

    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            p = u.path.rstrip("/") or "/"
            if p == "/":
                return self.send(results_page(q))
            if p == "/models":
                return self.send(models_page(q, q.pop("msg", "")))
            if p == "/categories":
                return self.send(categories_page(q.get("msg", "")))
            if p in ("/export.json", "/models.json"):
                return self.send(export(), ctype="application/json")
            if p == "/scans":
                return self.send(scans_page())
            if p == "/settings":
                return self.send(settings_page(q.get("msg", "")))
            m = re.fullmatch(r"/models/(\d+)", p)
            if m:
                with db() as con:
                    row = con.execute("SELECT * FROM models WHERE id=?", (int(m[1]),)).fetchone()
                if not row:
                    return self.send(page("Not found", "<p>No such watch.</p>"), 404)
                return self.send(page("Edit", "<p><a href='/models'>← Watches</a></p>" +
                                      model_form(dict(row), f"/models/{row['id']}", "Save"), "models"))
            self.send(page("Not found", "<p>Not found.</p>"), 404)
        except Exception:
            self.fault()

    def do_POST(self):
        # Only accept posts from this app's own pages, so another site open in
        # the same browser can't delete watches.
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
            return self.send("Cross-origin request refused.", 403, "text/plain")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            f = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode(), keep_blank_values=True))
            p = self.path.rstrip("/")
            if p == "/scan":
                threading.Thread(target=run_scan, args=("web",), daemon=True).start()
                return self.redirect(self.headers.get("Referer") or "/")
            if p == "/settings":
                with db() as con:
                    for k in DEFAULTS:
                        if k in f:
                            con.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                        (k, f[k].strip()))
                return self.redirect("/settings?msg=Saved.")
            if p == "/models":
                with db() as con:
                    data, err = parse_model(con, f)
                    if err:
                        con.rollback()
                        return self.send(models_page({}, form=f, err=err), 400)
                    con.execute("INSERT INTO models(label, terms, match, match_mode, exclude, max_buy, tier, notes, category_id) "
                                "VALUES (:label,:terms,:match,:match_mode,:exclude,:max_buy,:tier,:notes,:category_id)", data)
                return self.redirect(back(f"/models?cat={data['category_id']}", f"Added {data['label']}. It's in the next scan."))
            if p == "/categories":
                return self._category(None, f)
            m = re.fullmatch(r"/categories/(\d+)(/delete)?", p)
            if m:
                return self._category(int(m[1]), f, delete=bool(m[2]))
            m = re.fullmatch(r"/models/(\d+)(/delete|/toggle|/term-add|/term-remove)?", p)
            if m:
                return self._model(int(m[1]), m[2], f)
            self.send("Not found", 404, "text/plain")
        except Exception:
            self.fault()

    def _category(self, cid, f, delete=False):
        with db() as con:
            if delete:
                c = con.execute("SELECT name, (SELECT COUNT(*) FROM models WHERE category_id=?) AS n FROM categories WHERE id=?",
                                (cid, cid)).fetchone()
                if not c:
                    return self.redirect("/categories")
                if c["n"]:
                    return self.send(categories_page(err=f"{c['name']} still has {c['n']} watches. Move or delete them first."), 400)
                con.execute("DELETE FROM categories WHERE id=?", (cid,))
                return self.redirect(back("/categories", f"Deleted {c['name']}."))
            name = f.get("name", "").strip()
            if not name:
                return self.send(categories_page(err="A category needs a name."), 400)
            clash = con.execute("SELECT id FROM categories WHERE name=? AND id IS NOT ?", (name, cid)).fetchone()
            if clash:
                return self.send(categories_page(err=f"There's already a category called {name}."), 400)
            if cid is None:
                con.execute("INSERT INTO categories(name, notes) VALUES (?,?)", (name, f.get("notes", "").strip()))
                return self.redirect(back("/categories", f"Added {name}."))
            con.execute("UPDATE categories SET name=?, notes=? WHERE id=?", (name, f.get("notes", "").strip(), cid))
            return self.redirect(back("/categories", f"Saved {name}."))

    def _model(self, mid, act, f):
        with db() as con:
            row = con.execute("SELECT * FROM models WHERE id=?", (mid,)).fetchone()
            if not row:
                return self.redirect(back("/models", "That watch no longer exists."))
            here = f"/models?cat={row['category_id']}"
            if act == "/delete":
                con.execute("DELETE FROM models WHERE id=?", (mid,))
                return self.redirect(back(here, f"Deleted {row['label']}."))
            if act == "/toggle":
                con.execute("UPDATE models SET enabled = 1 - enabled WHERE id=?", (mid,))
                return self.redirect(here)
            if act in ("/term-add", "/term-remove"):
                terms = term_list(row["terms"])
                t = " ".join(f.get("term", "").split())
                if act == "/term-add":
                    new = term_list("\n".join(terms + [x.strip() for x in t.split(",")]))
                    added = len(new) - len(terms)
                    msg = (f"Added to {row['label']}." if added else f"{row['label']} already has that keyword.") if t else "Type a keyword first."
                else:
                    new = [x for x in terms if x.lower() != t.lower()]
                    if not new:
                        return self.redirect(back(here, f"{row['label']} needs at least one keyword. Delete the watch instead."))
                    msg = f"Removed “{t}” from {row['label']}."
                con.execute("UPDATE models SET terms=? WHERE id=?", ("\n".join(new), mid))
                refile(con, mid)
                return self.redirect(back(here, msg))
            data, err = parse_model(con, f)
            if err:
                con.rollback()
                return self.send(page("Edit", model_form(f, f"/models/{mid}", "Save", err), "models"), 400)
            data["id"] = mid
            con.execute("UPDATE models SET label=:label, terms=:terms, match=:match, match_mode=:match_mode, exclude=:exclude, "
                        "max_buy=:max_buy, tier=:tier, notes=:notes, category_id=:category_id WHERE id=:id", data)
            refile(con, mid)
            return self.redirect(back(f"/models?cat={data['category_id']}", f"Saved {data['label']}."))


def export():
    with db() as con:
        cats = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM categories")}
        return json.dumps([{"category": cats.get(r["category_id"]), "label": r["label"], "keywords": term_list(r["terms"]),
                            "match_mode": r["match_mode"], "match": r["match"], "exclude": r["exclude"],
                            "max_buy": r["max_buy"], "tier": r["tier"], "notes": r["notes"],
                            "enabled": bool(r["enabled"])}
                           for r in con.execute("SELECT * FROM models ORDER BY category_id, id")], indent=2)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8088)
    sub.add_parser("scan")
    sub.add_parser("export")
    a = ap.parse_args()
    init()
    if a.cmd == "serve":
        print(f"sgw-watch on http://{a.host}:{a.port}  db={DB_PATH}", flush=True)
        ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    elif a.cmd == "scan":
        msg = run_scan("timer" if os.environ.get("INVOCATION_ID") else "cli")
        print(msg)
        sys.exit(1 if msg.startswith("Scan failed") else 0)
    elif a.cmd == "export":
        print(export())


if __name__ == "__main__":
    main()
