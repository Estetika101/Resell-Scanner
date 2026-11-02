"""ShopGoodwill client, stdlib only.

Ported from the retired scanner's adapter (flip: archive/scanner/sources/
shopgoodwill.py), which was VERIFIED against live traffic 2026-08-14. The
four things that adapter found the hard way are kept, each at its site:

  1. endTime is naive US/Pacific, not UTC          -> parse_end
  2. there is no isBuyNow field; listingType 0 = auction
  3. CalculateShipping returns an HTML string, not JSON -> shipping_quote
  4. image paths can be relative and backslashed  -> image_url

The 1.5 s delay between requests is the old adapter's too. Do not lower it:
roger's residential IP is the asset, and a blocked IP ends the tool.
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Overridable so the test suite can point it at a local mock.
API_ROOT = os.environ.get("SGW_API_ROOT", "https://buyerapi.shopgoodwill.com/api")
ITEM_URL = "https://shopgoodwill.com/item/{}"
IMAGE_ROOT = "https://shopgoodwillimages.azureedge.net/production/"
SITE_TZ = ZoneInfo("America/Los_Angeles")
DELAY = float(os.environ.get("SGW_DELAY", "1.5"))

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Origin": "https://shopgoodwill.com",
    "Referer": "https://shopgoodwill.com/",
}

# Mirrors the payload the site's own frontend sends. Unused keys are kept
# because the endpoint has historically rejected partial bodies.
SEARCH_TEMPLATE = {
    "isSize": False, "isWeddingCatagory": "false", "isMultipleCategoryIds": False,
    "isFromHeaderMenuTab": False, "layout": "grid", "isFromHomePage": False,
    "searchText": "", "selectedGroup": "", "selectedCategoryIds": "",
    "selectedSellerIds": "", "lowPrice": "0", "highPrice": "999999",
    "searchBuyNowOnly": "", "searchPickupOnly": "false", "searchNoPickupOnly": "false",
    "searchOneCentShippingOnly": "false",
    # Titles only. The old scanner searched descriptions too, but this tool
    # matches on the model number in the title, so description hits are noise.
    "searchDescriptions": "false",
    "searchClosedAuctions": "false", "closedAuctionEndingDate": "",
    "closedAuctionDaysBack": "0", "searchCanadaShipping": "false",
    "searchInternationalShippingOnly": "false",
    "sortColumn": "1", "page": "1", "pageSize": "40", "sortDescending": "false",
    "savedSearchId": 0, "useBuyerPrefs": "true", "searchUSOnlyShipping": "true",
    "categoryLevelNo": "1", "categoryLevel": 1, "categoryId": 0,
    "partNumber": "", "catIds": "",
}


class ShapeError(RuntimeError):
    """The API answered, but not in the shape we verified. Don't guess."""


_last = [0.0]


def _wait():
    gap = time.monotonic() - _last[0]
    if gap < DELAY:
        time.sleep(DELAY - gap)
    _last[0] = time.monotonic()


def _post(path: str, body: dict):
    _wait()
    req = urllib.request.Request(f"{API_ROOT}{path}", data=json.dumps(body).encode(),
                                 headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def search(term: str, pages: int = 2) -> list[dict]:
    """Live listings for one term, ending soonest first, as plain dicts."""
    out = []
    for page in range(1, pages + 1):
        body = dict(SEARCH_TEMPLATE, searchText=term, page=str(page))
        data = _post("/Search/ItemListing", body)
        if not isinstance(data, dict) or "searchResults" not in data:
            keys = list(data)[:10] if isinstance(data, dict) else type(data).__name__
            raise ShapeError(f"ItemListing response shape changed; top keys: {keys}")
        sr = data.get("searchResults") or {}
        items = sr.get("items") or []
        out.extend(parse(i) for i in items)
        total = sr.get("itemCount")
        if not items or (total is not None and len(out) >= total):
            break
    return out


def parse(raw: dict) -> dict:
    listing_type = int(raw.get("listingType") or 0)
    buy_now = money(raw.get("buyNowPrice"))
    price = money(raw.get("currentPrice") or raw.get("minimumBid") or 0)
    if listing_type != 0 and buy_now:
        price = buy_now
    return {
        "item_id": str(raw.get("itemId") or raw.get("id")),
        "title": raw.get("title") or "",
        "price": price,
        "bids": raw.get("numBids") or raw.get("bidCount") or 0,
        "is_auction": listing_type == 0,
        "end_utc": parse_end(raw.get("endTime")),
        "image": image_url(raw.get("imageURL")),
        "seller": raw.get("sellerName") or "",
    }


def shipping_quote(item_id: str, zipcode: str, state: str = "TX"):
    """(shipping, handling) from ShopGoodwill's own calculator, or None.

    The endpoint returns a JSON-encoded HTML STRING, e.g.
      "<p>Shipping: <span id='shipping-span'>$13.57 (GROUND_HOME_DELIVERY)</span>
       </p><p>Handling: $3.49</p>..."
    None means no rate was quoted (pickup only, or the calculator declined).
    Callers must treat None as unknown, never as free.
    """
    try:
        html = _post("/ItemDetail/CalculateShipping", {
            "itemId": int(item_id), "country": "US", "province": state,
            "zipCode": zipcode, "quantity": 1, "clientIP": "",
        })
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    if not isinstance(html, str):
        raise ShapeError(f"CalculateShipping returned {type(html).__name__}, expected str")
    ship = _first_money(html, r"shipping-span[^>]*>\s*\$([\d,]+\.\d{2})")
    if ship is None:
        return None
    return ship, _first_money(html, r"Handling:\s*\$([\d,]+\.\d{2})") or 0.0


def money(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return float(str(v).replace("$", "").replace(",", "").strip() or 0)


def _first_money(text, pattern):
    m = re.search(pattern, text, re.I)
    return money(m.group(1)) if m else None


def image_url(v) -> str:
    if not v:
        return ""
    p = str(v).split(";")[0].replace("\\", "/").strip()
    return p if p.startswith("http") else IMAGE_ROOT + p.lstrip("/")


def parse_end(v):
    """API timestamp -> ISO UTC string. The API sends naive US/Pacific;
    reading it as UTC shifts every close 7-8 hours early."""
    if not v:
        return None
    s = str(v).replace("Z", "+00:00")
    dt = None
    for fmt in (None, "%m/%d/%Y %I:%M:%S %p", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.fromisoformat(s) if fmt is None else datetime.strptime(str(v), fmt)
            break
        except (ValueError, TypeError):
            continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SITE_TZ)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
