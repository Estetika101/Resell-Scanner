"""Claude Haiku pass over a seller's other listings. Stdlib only.

Suggests what might be worth a look and says why. It never prices anything:
Peter's own sales showed most losses came from misvaluing, so a number from a
model would be the dangerous part. A suggestion becomes a buy only when he
turns it into a watch with his own max.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

API = os.environ.get("ANTHROPIC_API_ROOT", "https://api.anthropic.com") + "/v1/messages"

DEFAULT_PROFILE = """I resell secondhand electronics on eBay, Facebook Marketplace and Depop.
Proven sellers for me: vintage CCD-era digital point-and-shoot cameras (Canon PowerShot/ELPH/SX, Sony Cyber-shot W/T/S/P, Nikon Coolpix, Fujifilm FinePix F-series, Panasonic Lumix, Olympus FE, Kodak Zi8), projectors, vintage game consoles, controllers and cartridges (NES, SNES, N64, Game Boy, Sega), and old iPods (Classic, Video, Mini).
Never suggest: film cameras, bridge/superzoom cameras, Casio Exilim, Olympus Stylus/mju, Flip or PlayStation-branded pocket video cameras (Kodak Zi8 is the exception), prosumer camera bodies, parts-only or untested lots, multi-unit audio lots, clothing, furniture, books, jewelry."""

SYSTEM = """You help a reseller decide which other listings from the same ShopGoodwill seller are worth a look, so several wins can ship together.
Pick only items that fit the reseller's profile. Be strict: most listings are not a fit, and picking nothing is a fine answer.
Never estimate prices or values. Never say an item is profitable. Say only what the item is and why it fits the profile.
Reply with JSON only, no prose: {"picks": [{"id": "<id from the list>", "category": "<short category>", "reason": "<one short sentence>"}]}"""


class AIError(RuntimeError):
    pass


def suggest(key: str, model: str, profile: str, items: list[dict], max_tokens: int = 2000):
    """items: [{"id", "title", "price"}]. Returns (picks, usage) where picks only
    name ids that were sent. Raises AIError with a readable reason on failure."""
    if not items:
        return [], {"input_tokens": 0, "output_tokens": 0}
    listing = "\n".join(f'{i["id"]}\t{i["title"]}' for i in items)
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": SYSTEM + "\n\nReseller profile:\n" + (profile.strip() or DEFAULT_PROFILE),
        "messages": [{"role": "user", "content": "Listings (id<TAB>title):\n" + listing}],
    }
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.load(e).get("error", {}).get("message", "")
        except Exception:
            pass
        reason = {401: "the API key was refused", 403: "the API key isn't allowed to use this model",
                  404: f"model {model!r} wasn't found", 429: "rate limited or out of credit"}.get(e.code, f"HTTP {e.code}")
        raise AIError(f"Claude API: {reason}{(' (' + detail + ')') if detail else ''}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise AIError(f"Claude API unreachable: {e}") from None

    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise AIError("Claude's reply had no JSON in it.")
    try:
        picks = json.loads(m.group(0)).get("picks", [])
    except json.JSONDecodeError:
        raise AIError("Claude's reply wasn't valid JSON.") from None
    sent = {str(i["id"]) for i in items}
    clean = []
    for p in picks if isinstance(picks, list) else []:
        pid = str(p.get("id", "")).strip()
        if pid in sent:  # never trust an id we didn't send
            clean.append({"id": pid, "category": str(p.get("category", ""))[:40],
                          "reason": str(p.get("reason", ""))[:200]})
    usage = data.get("usage", {})
    return clean, {"input_tokens": int(usage.get("input_tokens", 0)), "output_tokens": int(usage.get("output_tokens", 0))}
