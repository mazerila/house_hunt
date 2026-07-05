"""Step 3 — find alternative listings for the same property via web search.

Searches the property's key facts on a search engine and keeps result links that
point at known French real-estate portals; the caller runs steps 1+2 on each to
add missing data / surface discrepancies.

Uses DuckDuckGo's HTML endpoint: Google blocks automated search (instant
reCAPTCHA), whereas DuckDuckGo's lite/HTML interface returns plain, parseable
results with no account and no challenge.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

import cache, settings

DDG_HTML = "https://html.duckduckgo.com/html/"

# host substring -> display name. Detail pages on these are candidate alternatives.
PORTALS = {
    "seloger.com": "SeLoger",
    "leboncoin.fr": "Leboncoin",
    "bienici.com": "Bien'ici",
    "pap.fr": "PAP",
    "logic-immo.com": "Logic-Immo",
    "figaro-immobilier.fr": "Figaro Immobilier",
    "properstar.fr": "Properstar",
    "paruvendu.fr": "ParuVendu",
    "ouestfrance-immo.com": "Ouest-France Immo",
    "immobilier.notaires.fr": "Notaires",
    "green-acres.fr": "Green-Acres",
    "iadfrance.fr": "iad",
    "bellesdemeures.com": "Belles Demeures",
    "explorimmo.com": "Explorimmo",
    "avendrealouer.fr": "AVendreALouer",
    "superimmo.com": "Superimmo",
}


def _build_query(fields: dict) -> str:
    bits: list[str] = []
    if fields.get("address"):
        bits.append(str(fields["address"]))
    if fields.get("property_type"):
        bits.append(str(fields["property_type"]))
    if fields.get("surface_m2"):
        bits.append(f"{int(float(fields['surface_m2']))} m2")
    if fields.get("asking_price"):
        bits.append(f"{int(fields['asking_price'])} €")
    return " ".join(bits).strip()


def _portal_for(url: str) -> str | None:
    host = urlsplit(url).netloc.lower()
    for key, name in PORTALS.items():
        if key in host:
            return name
    return None


_SEARCH_PATH = ("/recherche", "/search", "/annonces-immobilieres", "/louer/", "/acheter/")


def _is_detail(url: str) -> bool:
    """Heuristic: a real listing detail page, not a search/index page."""
    low = url.lower()
    if any(s in low for s in _SEARCH_PATH):
        return False
    return re.search(r"\d{5,}", urlsplit(url).path) is not None  # a listing id


def _ddg_links(html: str) -> list[str]:
    out: list[str] = []
    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', html):
        if "uddg=" in href:
            u = parse_qs(urlsplit(href).query).get("uddg", [None])[0]
            if u:
                href = unquote(u)
        if href.startswith("http"):
            out.append(href)
    return out


async def _search(query: str) -> list[str] | None:
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:130.0) "
                       "Gecko/20100101 Firefox/130.0"),
        "Accept-Language": "fr-FR,fr;q=0.8,en;q=0.5",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True,
                                     headers=headers) as c:
            r = await c.get(DDG_HTML, params={"q": query, "kl": "fr-fr"})
            r.raise_for_status()
            return _ddg_links(r.text)
    except Exception:  # noqa: BLE001 - search is best-effort
        return None


async def find_alternatives(fields: dict | None, exclude_url: str,
                            max_n: int = 2) -> list[dict]:
    if not fields:
        return []
    query = _build_query(fields)
    if not query:
        return []

    links = cache.get("search", query)
    if links is None:
        links = await _search(query)
        if links is not None:
            cache.set("search", query, links)
    links = links or []

    exclude = exclude_url.rstrip("/")
    seen_hosts, out = set(), []
    for url in links:
        portal = _portal_for(url)
        host = urlsplit(url).netloc.lower()
        if not portal or url.rstrip("/") == exclude or host in seen_hosts:
            continue
        if not _is_detail(url):
            continue
        seen_hosts.add(host)  # at most one listing per portal
        out.append({"url": url, "portal": portal, "why": "web search"})
        if len(out) >= max_n:
            break
    return out
