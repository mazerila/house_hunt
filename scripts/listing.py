"""Listing extraction.

  Step 1  fetch_page  -> download a listing URL and reduce it to a textual
                         representation (page text + a few photos).
          summarize   -> turn that (text + photos via vision) into a clean
                         free-text summary of the property.
  Step 2  structurize -> fill the ListingFields data model from the summary
                         (plain prompting + Pydantic; no strict-schema API).

`extract_one` runs both steps for one URL. Everything starts from scratch — no
caching — so one property's data can never leak into another's.

Requires ANTHROPIC_API_KEY (loaded from .env by settings).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from urllib.parse import urljoin

import browser, cache, settings
from models import ListingFields

_PROMPT_VERSION = "v1"  # bump to invalidate all cached AI results


def _sha(*parts: str) -> str:
    return hashlib.sha256(" ".join(parts).encode("utf-8")).hexdigest()

# Fetches run through a real browser (Playwright, persistent profile): it
# executes JS (so SPA listings render) and gets past more anti-bot than raw HTTP.
_NAV_TIMEOUT = 35000      # ms — page.goto
_CHALLENGE_WAIT = 25000   # ms — total budget for an interstitial to clear
# Anti-bot interstitials that auto-resolve to the real page after a moment.
_CHALLENGE_MARKERS = (
    "verifying your device", "vérification", "checking your browser",
    "just a moment", "un instant", "captcha-delivery.com", "datadome",
    "/cdn-cgi/challenge", "px-captcha",
)
# A page still showing these (and little else) is a hard block, not a listing.
_BLOCK_MARKERS = _CHALLENGE_MARKERS + (
    "pardon our interruption", "access to this page has been denied",
    "verify you are a human", "vous êtes un robot", "unusual traffic",
)
_IMG_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGES = 4
MAX_IMG_BYTES = 4_000_000
MAX_TEXT = 60000

_SUMMARY_PROMPT = (
    "You are reviewing a French real-estate listing. Using ONLY this page's text "
    "and photos, write a concise factual summary in English (8-12 lines) covering: "
    "asking price, price per m², living surface (note any 'surface au sol' vs "
    "'surface habitable / loi Carrez' distinction), rooms/bedrooms, property type, "
    "location/address, DPE energy class and value, the agency/seller, and notable "
    "features or condition visible in the photos. State only what THIS page "
    "supports; write 'not stated' when unknown. Do not invent or use outside data."
)

_STRUCT_PROMPT = (
    "Convert the listing summary below into a single JSON object. Output ONLY the "
    "JSON, no prose, no code fences. Use null for anything unknown. Numbers must be "
    "plain (euros, m²) with no separators or units. Conform to this JSON Schema:"
)


def _reduce_html(html: str) -> str:
    ld = re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.I | re.S)
    metas = re.findall(
        r'<meta[^>]+(?:property|name)=["\'](og:[^"\']+|description|twitter:[^"\']+)["\']'
        r'[^>]+content=["\']([^"\']*)["\']', html, re.I)
    text = re.sub(r'<(script|style)\b.*?</\1>', ' ', html, flags=re.I | re.S)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    parts = []
    if metas:
        parts.append("META:\n" + "\n".join(f"{k} = {v}" for k, v in metas))
    if ld:
        parts.append("JSON-LD:\n" + "\n".join(s.strip() for s in ld))
    parts.append("PAGE TEXT:\n" + text)
    return "\n\n".join(parts)[:MAX_TEXT]


def _image_urls(html: str, base: str) -> list[str]:
    cands: list[str] = []
    cands += re.findall(
        r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image)["\']'
        r'[^>]+content=["\']([^"\']+)["\']', html, re.I)
    for block in re.findall(r'application/ld\+json[^>]*>(.*?)</script>', html, re.I | re.S):
        cands += re.findall(r'"(?:image|contentUrl)"\s*:\s*"([^"]+)"', block)
    seen, out = set(), []
    for u in cands:
        u = urljoin(base, u.strip().replace("&amp;", "&"))
        if u.startswith("http") and u not in seen:
            seen.add(u)
            out.append(u)
        if len(out) >= MAX_IMAGES:
            break
    return out


async def _settle(page) -> str:
    """Wait for any anti-bot interstitial ('Verifying your device', 'Just a
    moment', DataDome…) to auto-resolve, then return the final page HTML.

    Returns as soon as the page no longer looks like a challenge, or when the
    challenge budget is exhausted (caller decides if it's a hard block).
    """
    waited, step = 0, 2000
    html = await page.content()
    while waited < _CHALLENGE_WAIT and any(m in html.lower() for m in _CHALLENGE_MARKERS):
        try:
            await page.wait_for_load_state("networkidle", timeout=step)
        except Exception:  # noqa: BLE001
            await page.wait_for_timeout(step)
        waited += step
        html = await page.content()
    if not any(m in html.lower() for m in _CHALLENGE_MARKERS):
        # one more idle settle for the real content / lazy images
        try:
            await page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:  # noqa: BLE001
            pass
        html = await page.content()
    return html


async def fetch_page(url: str) -> tuple[str, list[tuple[str, str]]]:
    """Render the listing in the shared (persistent) browser -> (text, images).

    Waits out anti-bot interstitials and judges the FINAL rendered content, so a
    'Verifying your device' page resolves to the listing before we read it.
    """
    ctx = await browser.get_context()
    page = await ctx.new_page()
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
        status = resp.status if resp is not None else 0
        html = await _settle(page)
        text = _reduce_html(html)

        blocked = any(m in html.lower() for m in _BLOCK_MARKERS)
        if (status >= 400 or blocked) and len(text) < 1500:
            raise RuntimeError(
                f"blocked by anti-bot (HTTP {status or 'n/a'}); the challenge did "
                "not clear. Set HOUSE_HUNT_HEADLESS=0 and solve it once in the "
                "browser window — the clearance is then remembered.")

        images = await _download_images(ctx, _image_urls(html, url))
        return text, images
    finally:
        await page.close()


async def _download_images(ctx, urls: list[str]) -> list[tuple[str, str]]:
    """Fetch images through the browser context (shares cookies / anti-bot)."""
    out: list[tuple[str, str]] = []
    for u in urls:
        try:
            r = await ctx.request.get(u, timeout=15000)
            if not r.ok:
                continue
            ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            body = await r.body()
            if ct in _IMG_TYPES and 0 < len(body) <= MAX_IMG_BYTES:
                out.append((ct, base64.standard_b64encode(body).decode()))
        except Exception:  # noqa: BLE001 - skip any image that fails
            continue
    return out


def _extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1:
        text = text[s:e + 1]
    return json.loads(text)


def _summarize_sync(client, text: str, images: list[tuple[str, str]], url: str) -> str:
    # Content-addressed cache: skip the LLM if the page text + photos are
    # unchanged from a previous run.
    img_sig = _sha(*(ct + b for ct, b in images)) if images else "noimg"
    key = _sha(settings.ANTHROPIC_MODEL, _PROMPT_VERSION, "summary", img_sig, text)
    hit = cache.get("ai_summary", key)
    if hit is not None:
        return hit

    blocks: list[dict] = [
        {"type": "image", "source": {"type": "base64", "media_type": ct, "data": b64}}
        for ct, b64 in images
    ]
    blocks.append({"type": "text", "text": f"{_SUMMARY_PROMPT}\n\nURL: {url}\n\n{text}"})
    resp = client.messages.create(model=settings.ANTHROPIC_MODEL, max_tokens=1024,
                                  output_config={"effort": "low"},
                                  messages=[{"role": "user", "content": blocks}])
    summary = "".join(b.text for b in resp.content if b.type == "text").strip()
    cache.set("ai_summary", key, summary)
    return summary


def _structurize_sync(client, summary: str) -> ListingFields:
    key = _sha(settings.ANTHROPIC_MODEL, _PROMPT_VERSION, "structure", summary)
    hit = cache.get("ai_structure", key)
    if hit is not None:
        return ListingFields.model_validate(hit)

    schema = json.dumps(ListingFields.model_json_schema())
    resp = client.messages.create(
        model=settings.ANTHROPIC_MODEL, max_tokens=1024,
        output_config={"effort": "low"},
        messages=[{"role": "user",
                   "content": f"{_STRUCT_PROMPT}\n{schema}\n\nListing summary:\n{summary}"}])
    raw = "".join(b.text for b in resp.content if b.type == "text")
    fields = ListingFields.model_validate(_extract_json(raw))
    cache.set("ai_structure", key, fields.model_dump())
    return fields


def _both_sync(text: str, images: list[tuple[str, str]], url: str) -> tuple[ListingFields, str]:
    import anthropic
    client = anthropic.Anthropic()
    summary = _summarize_sync(client, text, images, url)
    return _structurize_sync(client, summary), summary


async def extract_one(url: str) -> dict:
    """Run steps 1+2 for one URL. Returns a plain dict (never raises)."""
    try:
        text, images = await fetch_page(url)
    except Exception as e:  # noqa: BLE001
        return {"url": url, "ok": False, "error": f"could not fetch page: {e}",
                "fields": None, "summary": None, "image_count": 0}
    try:
        fields, summary = await asyncio.to_thread(_both_sync, text, images, url)
    except Exception as e:  # noqa: BLE001
        return {"url": url, "ok": False,
                "error": f"LLM extraction failed (is ANTHROPIC_API_KEY set?): {e}",
                "fields": None, "summary": None, "image_count": len(images)}
    return {"url": url, "ok": True, "error": None, "fields": fields.model_dump(),
            "summary": summary, "image_count": len(images)}
