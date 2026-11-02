#!/usr/bin/env python3
"""sgw-watch: ShopGoodwill camera watch list for roger. Stdlib only.

  python3 app.py serve [--host 0.0.0.0] [--port 8088]   web app
  python3 app.py scan                                   one scan pass (the timer runs this)
  python3 app.py export                                 models as JSON on stdout

Models (what to search for and the most you'll pay landed) are edited in the
web app and live in SQLite next to this file. Nothing here talks to
Marginspace or Neon.
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
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import sgw

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SGW_DB", os.path.join(HERE, "sgw-watch.db"))
LOCK_PATH = DB_PATH + ".scan.lock"
TIERS = ("winner", "new", "try")

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

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS models (
          id INTEGER PRIMARY KEY,
          label TEXT NOT NULL,
          terms TEXT NOT NULL,            -- one search term per line
          match TEXT NOT NULL DEFAULT '', -- optional title tokens, comma separated
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
        if con.execute("SELECT COUNT(*) FROM models").fetchone()[0] == 0 and \
           con.execute("SELECT COUNT(*) FROM settings WHERE key='seeded'").fetchone()[0] == 0:
            for label, terms, mx, tier, notes in SEED:
                con.execute("INSERT INTO models(label, terms, max_buy, tier, notes) VALUES (?,?,?,?,?)",
                            (label, "\n".join(terms), mx, tier, notes))
            con.execute("INSERT INTO settings VALUES ('seeded', ?)", (now_iso(),))
        # A scan killed mid-pass (reboot, timeout) must not read as running forever.
        con.execute("UPDATE scans SET status='failed', errors=errors || 'interrupted', finished=? "
                    "WHERE status='running' AND started < datetime('now', '-2 hours')", (now_iso(),))


def settings() -> dict:
    with db() as con:
        s = dict(DEFAULTS)
        s.update({r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")})
        return s


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def words(csv_text: str) -> list[str]:
    return [w.strip().lower() for w in csv_text.split(",") if w.strip()]


# ---------------------------------------------------------------- matching

def tokens_for(model) -> list[str]:
    """Title tokens a listing must contain to count as this model.

    The model's own `match` list if set, else the last word of each search
    term, with Sony's DSC- prefix dropped ("Sony DSC-W7" -> "w7", since
    listers write W7, DSC-W7 and DSCW7 alike).
    """
    if model["match"].strip():
        return words(model["match"])
    out = []
    for t in model["terms"].splitlines():
        if t.strip():
            tok = t.split()[-1].lower()
            out.append(re.sub(r"^dsc-?", "", tok))
    return out


def title_matches(title: str, toks: list[str]) -> bool:
    """Digit-bounded, so W7 doesn't catch W70 and F30 doesn't catch F300."""
    t = title.lower()
    return any(re.search(rf"(?<!\d){re.escape(tok)}(?!\d)", t) for tok in toks)


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
    jobs = [(m, t.strip()) for m in models for t in m["terms"].splitlines() if t.strip()]
    errors, matched, seen = [], 0, set()
    started = now_iso()

    for n, (m, term) in enumerate(jobs, 1):
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
        toks = tokens_for(m)
        with db() as con:
            for li in results:
                t = li["title"].lower()
                if li["item_id"] in seen or any(w in t for w in skip):
                    continue
                if not title_matches(li["title"], toks):
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
              ORDER BY l.end_utc LIMIT ?""", (now_iso(), int(s["max_quotes"]))).fetchall()
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
        con.execute("DELETE FROM listings WHERE end_utc < datetime('now', '-1 day')")
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

def rows(show_all=False, model_id=None):
    s = settings()
    est = float(s["ship_estimate"])
    q = """SELECT l.*, m.label, m.tier, m.max_buy, m.enabled FROM listings l
           JOIN models m ON m.id = l.model_id WHERE l.end_utc > ?"""
    args = [now_iso()]
    if model_id:
        q += " AND m.id = ?"
        args.append(model_id)
    out = []
    with db() as con:
        for r in con.execute(q + " ORDER BY l.end_utc", args):
            r = dict(r)
            if r["ship_status"] == "quoted":
                ship = (r["shipping"] or 0) + (r["handling"] or 0)
                r["ship_known"] = True
            else:
                ship = est
                r["ship_known"] = False
            r["ship_total"] = ship
            if r["ship_status"] == "none":
                # The calculator declined (often pickup only). Unknown, not free:
                # never let it read as clean.
                r["flags"] = ", ".join(x for x in (r["flags"], "no shipping quote") if x)
            r["landed"] = r["price"] + ship
            r["headroom"] = r["max_buy"] - r["landed"]
            r["bid_up_to"] = r["max_buy"] - ship
            if show_all or r["headroom"] >= 0:
                out.append(r)
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
                         "Click": f"https://shopgoodwill.com/item/{r['item_id']}", "Tags": "camera"})
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
    left = dt - datetime.now(timezone.utc)
    secs = int(left.total_seconds())
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
    dt = datetime.fromisoformat(iso)
    return dt.astimezone(ZoneInfo(s["timezone"])).strftime("%a %b %-d, %-I:%M %p")


# ---------------------------------------------------------------- web

CSS = """
:root{--bg:#f6f6f4;--fg:#1d1d1b;--mut:#6b6b66;--card:#fff;--line:#e4e4df;--acc:#0b57d0;--ok:#137333;--bad:#b3261e;--warn:#8a5a00;--chip:#efefea}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ececea;--mut:#9a9a94;--card:#1d1d1b;--line:#2e2e2b;--acc:#8ab4f8;--ok:#6dd58c;--bad:#f2b8b5;--warn:#f0c674;--chip:#2a2a27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,sans-serif}
header{display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--card)}
header b{font-size:16px}nav a{margin-right:14px;color:var(--fg);text-decoration:none}nav a.on{color:var(--acc);font-weight:600}
main{padding:16px;max-width:1200px;margin:0 auto}a{color:var(--acc)}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 14px}.mut{color:var(--mut)}small{color:var(--mut)}
button,.btn{font:inherit;padding:7px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;text-decoration:none;display:inline-block}
button.pri{background:var(--acc);border-color:var(--acc);color:#fff}button.del{color:var(--bad)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pos{color:var(--ok);font-weight:600}.neg{color:var(--bad)}.flag{color:var(--bad);font-size:13px}.est{color:var(--warn)}
.chip{display:inline-block;padding:1px 7px;border-radius:99px;background:var(--chip);font-size:12px}
img.th{width:56px;height:56px;object-fit:cover;border-radius:6px;background:var(--chip)}
form.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;display:grid;gap:10px;max-width:640px}
label{display:grid;gap:4px;font-size:13px;color:var(--mut)}input,textarea,select{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg)}
textarea{min-height:110px}.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}.msg{padding:10px 12px;border-radius:8px;background:var(--chip);margin-bottom:12px}
.err{color:var(--bad)}.scroll{overflow-x:auto;border-radius:10px}.show-sm{display:none}h2{font-size:15px;margin:22px 0 8px}.off td{opacity:.5}.inline{display:inline}
@media (max-width:720px){.hide-sm{display:none}.show-sm{display:block}td,th{padding:7px 5px;font-size:13px}.row{grid-template-columns:1fr}
header{gap:8px}header .mut{margin-left:0!important;width:100%;font-size:13px}td small{font-size:11px}}
"""


def e(v):
    return html.escape(str(v if v is not None else ""), quote=True)


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
                  for h, t, a in (("/", "Results", "results"), ("/models", "Models", "models"),
                                  ("/scans", "Scan log", "scans"), ("/settings", "Settings", "settings")))
    meta = f"<meta http-equiv='refresh' content='{refresh}'>" if refresh else ""
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>{meta}
<title>{e(title)} · SGW watch</title><style>{CSS}</style></head><body>
<header><b>SGW watch</b><nav>{nav}</nav><span class='mut' style='margin-left:auto'>{status}</span>
<form method=post action=/scan class=inline><button class=pri>Scan now</button></form></header>
<main>{body}</main></body></html>"""


def results_page(q):
    s = settings()
    show_all = q.get("all") == "1"
    mid = q.get("model")
    rs = rows(show_all, int(mid) if mid and mid.isdigit() else None)
    with db() as con:
        models = con.execute("SELECT id, label FROM models ORDER BY label").fetchall()
    opts = "".join(f"<option value='{m['id']}' {'selected' if mid == str(m['id']) else ''}>{e(m['label'])}</option>"
                   for m in models)

    def table(items, empty):
        if not items:
            return f"<p class=mut>{empty}</p>"
        trs = []
        for r in items:
            ship = (f"${r['ship_total']:.2f}" if r["ship_known"]
                    else f"<span class=est title='Estimate: no real quote'>~${r['ship_total']:.0f}</span>")
            room = f"<span class='{'pos' if r['headroom'] >= 0 else 'neg'}'>${r['headroom']:.0f}</span>"
            upto = f"${r['bid_up_to']:.2f}" if r["bid_up_to"] > r["price"] else "<span class=neg>—</span>"
            fl = f"<div class=flag>{e(r['flags'])}</div>" if r["flags"] else ""
            img = f"<img class=th src='{e(r['image'])}' loading=lazy alt=''>" if r["image"] else ""
            kind = "" if r["is_auction"] else " <span class=chip>buy now</span>"
            trs.append(f"""<tr><td class=hide-sm>{img}</td>
<td><a href='https://shopgoodwill.com/item/{e(r['item_id'])}' target=_blank rel=noopener>{e(r['title'])}</a>{kind}{fl}
<div><small>{e(r['label'])} · <span class=chip>{e(r['tier'])}</span></small></div>
<div class=show-sm><small>Ends {e(fmt_end(r['end_utc'], s))}</small></div></td>
<td class=n>${r['price']:.2f}<br><small>{r['bids'] or 0} bids</small></td><td class='n hide-sm'>{ship}</td>
<td class=n>${r['landed']:.2f}</td><td class='n hide-sm'>${r['max_buy']:.0f}</td><td class=n>{room}</td>
<td class=n>{upto}</td><td class='n hide-sm'><small>{e(fmt_end(r['end_utc'], s))}</small></td></tr>""")
        return ("<div class=scroll><table><tr><th class=hide-sm></th><th>Listing</th><th class=n>Bid</th>"
                "<th class='n hide-sm'>Ship</th><th class=n>Landed</th><th class='n hide-sm'>Max</th>"
                "<th class=n>Room</th><th class=n>Bid up to</th><th class='n hide-sm'>Ends</th></tr>" + "".join(trs) + "</table></div>")

    clean = [r for r in rs if not r["flags"]]
    flagged = [r for r in rs if r["flags"]]
    body = f"""<form class=bar method=get>
<select name=model onchange='this.form.submit()'><option value=''>All models</option>{opts}</select>
<label style='display:flex;gap:6px;align-items:center'><input type=checkbox name=all value=1 {'checked' if show_all else ''}
 onchange='this.form.submit()'> Show over max</label>
<span class=mut>Ending soonest first. Landed = bid + shipping + handling. Check photos (battery, screen, lens, haze) before bidding.</span></form>
<h2>Clean ({len(clean)})</h2>{table(clean, "Nothing under max buy right now.")}
<h2>Flagged: parts / repair / lot / untested ({len(flagged)})</h2>{table(flagged, "None.")}"""
    return page("Results", body, "results")


def models_page(msg="", form=None, err=""):
    with db() as con:
        ms = con.execute("""SELECT m.*, (SELECT COUNT(*) FROM listings l WHERE l.model_id=m.id AND l.end_utc > ?) AS live
                            FROM models m ORDER BY m.tier='try', m.tier='new', m.label""", (now_iso(),)).fetchall()
    trs = "".join(f"""<tr class='{'' if m['enabled'] else 'off'}'>
<td><b>{e(m['label'])}</b><div><small>{e(', '.join(t for t in m['terms'].splitlines() if t.strip()))}</small></div>
{f"<div><small>{e(m['notes'])}</small></div>" if m['notes'] else ''}</td>
<td><span class=chip>{e(m['tier'])}</span></td><td class=n>${m['max_buy']:.0f}</td><td class=n>{m['live']}</td>
<td class=n style='white-space:nowrap'>
<form method=post action='/models/{m['id']}/toggle' class=inline><button>{'Pause' if m['enabled'] else 'Resume'}</button></form>
<a class=btn href='/models/{m['id']}'>Edit</a>
<form method=post action='/models/{m['id']}/delete' class=inline onsubmit="return confirm('Delete {e(m['label'])}?')"><button class=del>Delete</button></form>
</td></tr>""" for m in ms)
    body = (f"<div class=msg>{e(msg)}</div>" if msg else "") + \
        "<div class=scroll><table><tr><th>Model</th><th>Tier</th><th class=n>Max landed</th><th class=n>Live</th><th></th></tr>" + trs + \
        "</table></div><h2>Add a model</h2>" + model_form(form or {}, "/models", "Add model", err) + \
        "<p class=mut>Export: <a href='/models.json'>models.json</a></p>"
    return page("Models", body, "models")


def model_form(f, action, label, err=""):
    tier = f.get("tier", "winner")
    opts = "".join(f"<option {'selected' if t == tier else ''}>{t}</option>" for t in TIERS)
    return f"""<form class=card method=post action='{action}'>{f"<div class=err>{e(err)}</div>" if err else ''}
<label>Name<input name=label required value='{e(f.get('label', ''))}' placeholder='Canon SX100/110/120 IS'></label>
<label>Search terms, one per line<textarea name=terms required placeholder='Canon SX110'>{e(f.get('terms', ''))}</textarea></label>
<div class=row><label>Max landed buy ($)<input name=max_buy type=number step=0.01 min=1 required value='{e(f.get('max_buy', ''))}'></label>
<label>Tier<select name=tier>{opts}</select></label></div>
<label>Title must contain (optional, comma separated)<input name=match value='{e(f.get('match', ''))}'
 placeholder='Blank = last word of each search term, e.g. SX110'></label>
<label>Notes<input name=notes value='{e(f.get('notes', ''))}'></label>
<div><button class=pri>{label}</button></div></form>"""


def scans_page():
    s = settings()
    with db() as con:
        sc = con.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 50").fetchall()
    trs = "".join(f"""<tr><td>{e(fmt_ago(x['started'], s))}</td><td>{e(x['trigger'])}</td>
<td class='{'pos' if x['status'] == 'ok' else 'err'}'>{e(x['status'])}</td><td class=n>{x['matches']}</td>
<td class=n>{x['quotes']}</td><td><small style='white-space:pre-wrap'>{e(x['errors'] or x['progress'])}</small></td></tr>"""
                  for x in sc)
    return page("Scan log", "<table><tr><th>Started</th><th>By</th><th>Status</th><th class=n>Matches</th>"
                "<th class=n>Quotes</th><th>Errors</th></tr>" + trs + "</table>", "scans")


def settings_page(msg=""):
    s = settings()
    f = lambda k, lab, t="text": f"<label>{lab}<input name={k} type={t} value='{e(s[k])}'></label>"
    body = (f"<div class=msg>{e(msg)}</div>" if msg else "") + f"""<form class=card method=post action=/settings>
<div class=row>{f('zipcode', 'Ship-to ZIP')}{f('state', 'State')}</div>
<div class=row>{f('ship_estimate', 'Shipping estimate until quoted ($)', 'number')}{f('max_quotes', 'Max shipping quotes per scan', 'number')}</div>
{f('timezone', 'Time zone')}
<div class=row>{f('ntfy_server', 'ntfy server')}{f('ntfy_topic', 'ntfy topic (blank = no push)')}</div>
<label>Red flags (comma separated; flagged, still shown)<textarea name=red_flags>{e(s['red_flags'])}</textarea></label>
<label>Skip words (comma separated; dropped)<textarea name=skip_words>{e(s['skip_words'])}</textarea></label>
<div><button class=pri>Save</button></div></form>"""
    return page("Settings", body, "settings")


def parse_model(f):
    terms = "\n".join(t.strip() for t in f.get("terms", "").splitlines() if t.strip())
    data = {"label": f.get("label", "").strip(), "terms": terms, "match": f.get("match", "").strip(),
            "tier": f.get("tier", "winner"), "notes": f.get("notes", "").strip()}
    try:
        data["max_buy"] = round(float(f.get("max_buy", "")), 2)
    except ValueError:
        return data, "Max landed buy must be a number."
    if not data["label"]:
        return data, "Name is required."
    if not terms:
        return data, "Add at least one search term."
    if data["max_buy"] <= 0:
        return data, "Max landed buy must be more than $0."
    if data["tier"] not in TIERS:
        data["tier"] = "winner"
    return data, ""


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

    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            p = u.path.rstrip("/") or "/"
            if p == "/":
                return self.send(results_page(q))
            if p == "/models":
                return self.send(models_page(q.get("msg", "")))
            if p == "/models.json":
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
                    return self.send(page("Not found", "<p>No such model.</p>"), 404)
                return self.send(page("Edit", f"<p><a href='/models'>← Models</a></p>" +
                                      model_form(dict(row), f"/models/{row['id']}", "Save"), "models"))
            self.send(page("Not found", "<p>Not found.</p>"), 404)
        except Exception:
            traceback.print_exc()
            self.send(page("Error", "<p class=err>Something broke. The traceback is in the service log "
                           "(journalctl -u sgw-watch).</p>"), 500)

    def do_POST(self):
        # Only accept posts from this app's own pages, so another site open in
        # the same browser can't delete models.
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
            return self.send("Cross-origin request refused.", 403, "text/plain")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            f = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode(), keep_blank_values=True))
            p = self.path.rstrip("/")
            if p == "/scan":
                threading.Thread(target=run_scan, args=("web",), daemon=True).start()
                return self.redirect("/")
            if p == "/models":
                data, err = parse_model(f)
                if err:
                    return self.send(models_page(form=f, err=err), 400)
                with db() as con:
                    con.execute("INSERT INTO models(label, terms, match, max_buy, tier, notes) "
                                "VALUES (:label,:terms,:match,:max_buy,:tier,:notes)", data)
                return self.redirect("/models?msg=" + urllib.parse.quote(f"Added {data['label']}. It's in the next scan."))
            if p == "/settings":
                with db() as con:
                    for k in DEFAULTS:
                        if k in f:
                            con.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                        (k, f[k].strip()))
                return self.redirect("/settings?msg=Saved.")
            m = re.fullmatch(r"/models/(\d+)(/delete|/toggle)?", p)
            if m:
                mid, act = int(m[1]), m[2]
                with db() as con:
                    row = con.execute("SELECT * FROM models WHERE id=?", (mid,)).fetchone()
                    if not row:
                        return self.redirect("/models?msg=That+model+no+longer+exists.")
                    if act == "/delete":
                        con.execute("DELETE FROM models WHERE id=?", (mid,))
                        return self.redirect("/models?msg=" + urllib.parse.quote(f"Deleted {row['label']}."))
                    if act == "/toggle":
                        con.execute("UPDATE models SET enabled = 1 - enabled WHERE id=?", (mid,))
                        return self.redirect("/models")
                    data, err = parse_model(f)
                    if err:
                        return self.send(page("Edit", model_form(f, f"/models/{mid}", "Save", err), "models"), 400)
                    data["id"] = mid
                    con.execute("UPDATE models SET label=:label, terms=:terms, match=:match, max_buy=:max_buy, "
                                "tier=:tier, notes=:notes WHERE id=:id", data)
                    # Re-file listings that no longer match this model's tokens.
                    toks = tokens_for(data)
                    for l in con.execute("SELECT item_id, title FROM listings WHERE model_id=?", (mid,)).fetchall():
                        if not title_matches(l["title"], toks):
                            con.execute("DELETE FROM listings WHERE item_id=?", (l["item_id"],))
                return self.redirect("/models?msg=" + urllib.parse.quote(f"Saved {data['label']}."))
            self.send("Not found", 404, "text/plain")
        except Exception:
            traceback.print_exc()
            self.send(page("Error", "<p class=err>Something broke. The traceback is in the service log "
                           "(journalctl -u sgw-watch).</p>"), 500)


def export():
    with db() as con:
        return json.dumps([{"label": r["label"], "terms": r["terms"].splitlines(), "match": r["match"],
                            "max_buy": r["max_buy"], "tier": r["tier"], "notes": r["notes"],
                            "enabled": bool(r["enabled"])}
                           for r in con.execute("SELECT * FROM models ORDER BY id")], indent=2)


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
