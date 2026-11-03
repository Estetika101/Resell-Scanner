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

- **Categories**: group watches (Cameras, iPods, Game systems…). Add, rename,
  delete (only when empty). You can also make one from the watch form.
- **Watches**: one per thing you buy. Each has:
  - **Search keywords**, one per line; each is one ShopGoodwill search. Add one
    from the list with the "Add keyword" box (comma separate several), remove
    one with its ×.
  - **How a title has to match**:
    *Automatic* (default for new watches: model number for a keyword that
    ends in one, all words otherwise), *Model number* (title has the last word
    of a keyword, `Canon SX110` → `SX110`, digit-bounded so W7 doesn't catch
    W70; the seeded camera watches use this), *All words* (title has every word
    of one keyword), or *Anything the search returns*. "Title must contain"
    overrides all of them.
  - **Exclude words**: listings with any of these are dropped for this watch
    (`nano, shuffle`). Settings has skip words and red flags for every watch.
  - **Max landed buy** (bid + shipping + handling), **tier**, **notes**.
- **Results**:
  - **Search titles** at the top filters as you type.
  - **Filter pills** (Category, Watch, Location, Ends, Price, Tier, Show) open a
    small panel. Ticking an option applies it immediately, the panel stays open
    so you can tick more, and each option shows how many listings it would
    give. Options with no listings are hidden. Several ticks in one pill mean
    "any of these"; different pills combine.
  - **Applied filters** show as chips under the bar: × removes one,
    "Clear all" removes the lot.
  - **Sort** (ending soonest is the default) and **Group** (clean/flagged,
    location, category, watch, ending day, none).
  - **⚡ Good buys ending soon**: clean, real shipping quote, at least $5 under
    your max, ending within 24 hours, soonest first. Both numbers are in
    Settings.
  - **Save this view** keeps the current filters as a tab. × on an active
    saved tab deletes it. Every view is also just a URL you can bookmark.
  - **+ New watch** adds a watch without leaving Results: keywords, category,
    max. Matching is automatic: a keyword ending in a model number (SX130)
    matches on that number, other keywords need every word.
- **Combining shipping**: every listing has "+N more from this seller", which
  opens everything you're watching from that seller, over-max included
  (shipping is usually what puts them over). Group by **Location** shows each
  seller with what shipping one by one would cost and whether they combine.
  The real combined rate isn't known until checkout, so check the seller's
  policy before counting on it.
- **Locations**: every seller seen, with city and state. Mark the ones you'd
  drive to as **Local**, then use "Local pickup only" in the Location filter.
  Seeded with the retired scanner's 135-seller map; Goodwill Central Texas
  (Austin) starts as local. New sellers are looked up once, during a scan.
- **Columns**: Bid now · Shipping (+ handling) · **Total now** (bid +
  shipping) · Your max (total) · **Room left** (max − total) · **Bid up to**
  (max − shipping: the highest bid that still lands under your max).
- **My bids**: what you're bidding on, grouped by seller, with what else that
  seller has that ends around the same time, so several wins can ship together.
  - Tracking: install the userscript from My bids (Tampermonkey) and every bid
    you place on ShopGoodwill is sent here; item pages also get a **Track on
    roger** button. Or drag the **Track on roger** bookmarklet, paste an item
    link, or tap ☆ on a Results row. All of these need roger reachable from the
    browser (home network).
  - Suggestions: roger reads the seller's live listings (sweep) and matches
    them against every watch. With an Anthropic API key in Settings, Claude
    Haiku reads the rest and picks what fits the "What you buy" profile, with a
    one-line reason. Claude never prices anything; a pick becomes a buy only
    when you make it a watch with your own max. Titles Claude has seen aren't
    sent again. Spend is shown on the page (Haiku 5.5: $0.10 / $0.50 per
    million tokens in / out, editable in Settings).
  - Sellers you're bidding with are re-swept after a scan when the last sweep
    is over 6 hours old; "Sweep now" does it at once.
- **Import**: Watches → Import takes a file in export.json's shape (preview
  first), or on roger: `python3 app.py import watchlists/more-watches-2026-11.json`
  (`--dry-run` to preview, `--replace` to update existing watches).
- **Search words**: ShopGoodwill ORs the words of a search, so roger sends a
  keyword's model number on its own (SX110), or a watch's own "Search
  ShopGoodwill for" words if set, and checks the full keyword itself. Scan
  log → Keywords shows what each search found.
- **Settings**: ZIP for shipping quotes, global red-flag and skip words, the
  good-buy thresholds, and an optional ntfy topic for a push when a new clean listing appears.
- **export.json** (link on the Watches page) exports everything.

Seeded from the Camera Sourcing Cheat Sheet (Nov 1, 2026). After that, this
app is the list: change max buys here, not in the doc. Upgrading from the
first version keeps your watches and puts them in a Cameras category.

## Where things are

| | |
|---|---|
| `app.py` | Web app, scan, database. `python3 app.py scan` runs one pass. |
| `sgw.py` | ShopGoodwill client, ported from the retired scanner's adapter (`flip: archive/scanner/sources/shopgoodwill.py`), keeping its verified fixes: Pacific end times, listingType, HTML shipping quotes. |
| `ai.py` | The Claude Haiku call for seller suggestions. |
| `watchlists/` | Importable watch lists with their rationale. |
| `sellers_seed.json` | Seller map from the retired scanner (flip `archive/scanner/sellers.yaml`), loaded once. |
| `sgw-watch.db` | Your models, settings, listings, scan log. Back it up if you care. |
| `tests/` | `python3 -m unittest discover tests`, against a local mock API. |

Logs: `journalctl -u sgw-watch` (web) and `journalctl -u sgw-watch-scan` (scans).

## Rules kept from the old scanner

- 1.5 s between requests. Roger's residential IP is what ShopGoodwill blocks
  on; don't lower it to make scans faster.
- Unknown shipping is unknown, never $0.
- If the API's response shape changes, the scan stops and says so in the
  Scan log rather than guessing field names.
