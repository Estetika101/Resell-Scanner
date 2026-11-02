# sgw-watch

A ShopGoodwill watch list that runs on roger and is used from a browser on
the home network. You keep a list of models (search terms + the most you'll
pay landed), it scans every 2 hours, gets the real shipping quote for
anything that could fit, and shows what's under max buy.

Standalone: Python 3 standard library and SQLite. No venv, no pip, no
Marginspace, no Neon.

## Install on roger

On roger (or any Linux box with systemd and Python 3.9+):

```bash
git clone git@github.com:Estetika101/Resell-Scanner.git ~/sgw-watch   # private: uses roger's GitHub SSH key
cd ~/sgw-watch && ./install.sh
```

Then open **http://roger.local:8088**. To update: `cd ~/sgw-watch && git pull && ./install.sh`. That
keeps your models and settings (the database is gitignored).

## Using it

- **Models**: add, edit, pause, delete. One search term per line. Max
  landed buy = bid + shipping + handling. By default a listing counts only
  if its title has the last word of a search term (`Canon SX110` → `SX110`),
  digit-bounded so W7 doesn't catch W70. Set "Title must contain" to
  override, e.g. `F31fd, F31 fd`.
- **Results**: live listings under max buy, ending soonest first. "Bid up
  to" is max buy minus that listing's real shipping and handling. An amber
  `~$12` means no quote yet (the estimate from Settings). A listing whose
  shipping the calculator refused is flagged `no shipping quote`, never clean.
- **Settings**: ZIP for shipping quotes, red-flag and skip words, and an
  optional ntfy topic for a push when a new clean listing appears.
- **models.json** (link on the Models page) exports the list.

Seeded from the Camera Sourcing Cheat Sheet (Nov 1, 2026). After that, this
app is the list: change max buys here, not in the doc.

## Where things are

| | |
|---|---|
| `app.py` | Web app, scan, database. `python3 app.py scan` runs one pass. |
| `sgw.py` | ShopGoodwill client, ported from the retired scanner's adapter (`flip: archive/scanner/sources/shopgoodwill.py`), keeping its verified fixes: Pacific end times, listingType, HTML shipping quotes. |
| `sgw-watch.db` | Your models, settings, listings, scan log. Back it up if you care. |
| `tests/` | `python3 -m unittest discover tests`, against a local mock API. |

Logs: `journalctl -u sgw-watch` (web) and `journalctl -u sgw-watch-scan` (scans).

## Rules kept from the old scanner

- 1.5 s between requests. Roger's residential IP is what ShopGoodwill blocks
  on; don't lower it to make scans faster.
- Unknown shipping is unknown, never $0.
- If the API's response shape changes, the scan stops and says so in the
  Scan log rather than guessing field names.
