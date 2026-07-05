#!/usr/bin/env python3
"""Extract everything the RNB (Référentiel National des Bâtiments) knows about
an address and save it to a JSON file.

The RNB is the French national building registry: every building has a unique,
permanent ID-RNB that acts as a pivot to cross-reference other databases (DPE,
BDNB, BD TOPO, fichiers fonciers, cadastre). See ../tools.md.

Pipeline, given an address string:
  1. Geocode it via the BAN (Base Adresse Nationale) -> coordinates + BAN key.
  2. Match buildings via the RNB address endpoint (`/buildings/address/`).
     If that returns nothing, fall back to the geographic "closest" endpoint
     around the geocoded point.
  3. For every matched RNB id, consult the full building record
     (`/buildings/{id}/?withPlots=1`) -> geometry, status, addresses,
     cadastral plots, and ext_ids (BD TOPO / BDNB links).
  4. Write the whole bundle (query + geocoding + raw search + full records)
     to a JSON file.

Pure stdlib (urllib/json) — no third-party dependencies.

Usage:
    scripts/rnb_lookup.py "26 Rue Marc Sangnier 92290 Châtenay-Malabry"
    scripts/rnb_lookup.py "12 rue X, 75001 Paris" -o out.json
    scripts/rnb_lookup.py "..." --radius 80 --from-email you@example.com

Exit status: 0 if at least one building was found, 1 if none, 2 on error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BAN_SEARCH = "https://api-adresse.data.gouv.fr/search/"
RNB_BASE = "https://rnb-api.beta.gouv.fr/api/alpha/buildings"
USER_AGENT = "house-hunt-rnb-lookup/1.0 (+https://github.com/; stdlib urllib)"

# Hard caps so a loose match can't fan out into thousands of requests.
MAX_PAGES = 20
MAX_BUILDINGS = 100


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class HTTPResult:
    def __init__(self, data=None, status=200, error=None):
        self.data = data
        self.status = status
        self.error = error


def http_get_json(url: str, retries: int = 3, pause: float = 0.15) -> HTTPResult:
    """GET a URL and parse JSON, with polite retry/backoff.

    Returns an HTTPResult; never raises for ordinary HTTP errors (e.g. a 404 on
    an inactive building) so the caller can record them and continue.
    """
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                return HTTPResult(data=json.loads(body), status=resp.status)
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} {e.reason}"
            # 429 = rate limited, 5xx = transient -> back off and retry
            if e.code == 429 or 500 <= e.code < 600:
                time.sleep(pause * (2 ** attempt) + 0.5)
                continue
            # 4xx (e.g. 404) is final
            return HTTPResult(status=e.code, error=last_err)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            last_err = str(e)
            time.sleep(pause * (2 ** attempt))
    return HTTPResult(error=last_err or "request failed")


def _with_params(url: str, params: dict) -> str:
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    if not clean:
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + urllib.parse.urlencode(clean)


def fetch_paginated(url: str, pause: float) -> tuple[list, dict, list]:
    """Follow `next` links on a list endpoint. Returns (results, first_meta, errors).

    `first_meta` is the first page minus its `results`/`next`/`previous` keys
    (so address-endpoint fields like cle_interop_ban/status/score_ban survive).
    """
    results: list = []
    errors: list = []
    first_meta: dict = {}
    page = 0
    next_url = url
    while next_url and page < MAX_PAGES and len(results) < MAX_BUILDINGS:
        res = http_get_json(next_url, pause=pause)
        if res.error or not isinstance(res.data, dict):
            errors.append({"url": next_url, "error": res.error or "bad payload",
                           "status": res.status})
            break
        if page == 0:
            first_meta = {k: v for k, v in res.data.items()
                          if k not in ("results", "next", "previous")}
        results.extend(res.data.get("results") or [])
        next_url = res.data.get("next")
        page += 1
        if next_url:
            time.sleep(pause)
    return results[:MAX_BUILDINGS], first_meta, errors


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def geocode_ban(address: str) -> dict:
    """Geocode via the BAN. Returns a normalized dict (may hold an error)."""
    url = _with_params(BAN_SEARCH, {"q": address, "limit": 1, "autocomplete": 0})
    res = http_get_json(url)
    if res.error or not isinstance(res.data, dict):
        return {"error": res.error or "bad payload", "status": res.status}
    feats = res.data.get("features") or []
    if not feats:
        return {"error": "no geocoding result"}
    f = feats[0]
    lon, lat = (f.get("geometry") or {}).get("coordinates", [None, None])
    props = f.get("properties") or {}
    return {
        "label": props.get("label"),
        "score": props.get("score"),
        "ban_id": props.get("id"),          # BAN interop key (cle_interop_ban)
        "type": props.get("type"),
        "citycode": props.get("citycode"),
        "postcode": props.get("postcode"),
        "lat": lat,
        "lon": lon,
        "raw_properties": props,
    }


def search_by_address(address: str, from_email: str, pause: float) -> dict:
    url = _with_params(f"{RNB_BASE}/address/", {"q": address, "from": from_email})
    results, meta, errors = fetch_paginated(url, pause)
    return {"endpoint": url, "meta": meta, "results": results, "errors": errors}


def search_closest(lat, lon, radius: int, from_email: str, pause: float) -> dict:
    url = _with_params(f"{RNB_BASE}/closest/", {
        "point": f"{lat},{lon}", "radius": radius, "from": from_email})
    results, meta, errors = fetch_paginated(url, pause)
    return {"endpoint": url, "results": results, "errors": errors}


def search_plot(plot_id: str, from_email: str, pause: float) -> dict:
    url = _with_params(f"{RNB_BASE}/plot/{urllib.parse.quote(plot_id)}/",
                       {"from": from_email})
    results, meta, errors = fetch_paginated(url, pause)
    return {"endpoint": url, "results": results, "errors": errors}


def consult_building(rnb_id: str, from_email: str, pause: float,
                     found_via: str = "") -> dict:
    url = _with_params(f"{RNB_BASE}/{urllib.parse.quote(rnb_id)}/",
                       {"withPlots": 1, "from": from_email})
    res = http_get_json(url, pause=pause)
    if res.error or not isinstance(res.data, dict):
        rec = {"rnb_id": rnb_id, "_error": res.error or "bad payload",
               "_status": res.status}
    else:
        rec = res.data
    if found_via:
        rec["_found_via"] = found_via
    return rec


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _ids_from(results) -> list[str]:
    out = []
    for r in results or []:
        rid = r.get("rnb_id") if isinstance(r, dict) else None
        if rid:
            out.append(rid)
    return out


def lookup(address: str, radius: int, from_email: str, pause: float,
           expand_plots: bool = True) -> dict:
    bundle: dict = {
        "query": {
            "address": address,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "rnb_api_base": RNB_BASE,
            "expand_plots": expand_plots,
        },
        "geocoding_ban": {},
        "rnb_address_search": {},
        "rnb_closest_search": None,
        "rnb_plot_search": {},
        "buildings": [],
    }

    bundle["geocoding_ban"] = geocode_ban(address)

    addr_search = search_by_address(address, from_email, pause)
    bundle["rnb_address_search"] = addr_search
    seeds = [(rid, "address") for rid in _ids_from(addr_search["results"])]

    # Fallback: nothing matched by address -> search around the geocoded point.
    geo = bundle["geocoding_ban"]
    if not seeds and geo.get("lat") is not None and geo.get("lon") is not None:
        closest = search_closest(geo["lat"], geo["lon"], radius, from_email, pause)
        bundle["rnb_closest_search"] = closest
        seeds = [(rid, "closest") for rid in _ids_from(closest["results"])]

    consulted: dict[str, dict] = {}

    def consult(rid: str, via: str) -> None:
        if rid in consulted or len(consulted) >= MAX_BUILDINGS:
            return
        rec = consult_building(rid, from_email, pause, found_via=via)
        consulted[rid] = rec
        time.sleep(pause)

    for rid, via in seeds:
        consult(rid, via)

    # Expand: pull every other building sharing a discovered cadastral plot.
    # This catches the back/landlocked building on a divided plot that the
    # street address alone never reaches.
    if expand_plots:
        plot_ids: list[str] = []
        for rec in list(consulted.values()):
            for plot in rec.get("plots") or []:
                pid = plot.get("id") if isinstance(plot, dict) else None
                if pid and pid not in plot_ids:
                    plot_ids.append(pid)
        for pid in plot_ids:
            if len(consulted) >= MAX_BUILDINGS:
                break
            ps = search_plot(pid, from_email, pause)
            bundle["rnb_plot_search"][pid] = ps
            for rid in _ids_from(ps["results"]):
                consult(rid, f"plot:{pid}")

    bundle["buildings"] = list(consulted.values())
    return bundle


def _slug(address: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", address.lower()).strip("_")
    return (s or "address")[:60]


def _summarize(bundle: dict) -> str:
    geo = bundle["geocoding_ban"]
    lines = []
    if geo.get("error"):
        lines.append(f"BAN geocoding: {geo['error']}")
    else:
        lines.append(f"BAN: {geo.get('label')} (score {geo.get('score')}) "
                     f"@ {geo.get('lat')},{geo.get('lon')}")
    meta = bundle["rnb_address_search"].get("meta", {})
    if meta:
        lines.append(f"RNB address match: status={meta.get('status')} "
                     f"score_ban={meta.get('score_ban')} "
                     f"cle_interop_ban={meta.get('cle_interop_ban')}")
    if bundle["rnb_closest_search"]:
        lines.append("RNB address match empty -> used 'closest' point fallback")
    if bundle["rnb_plot_search"]:
        lines.append(f"Plot expansion: searched {len(bundle['rnb_plot_search'])} "
                     f"parcel(s) for co-located buildings")
    bdgs = bundle["buildings"]
    lines.append(f"Buildings retrieved: {len(bdgs)}")
    for b in bdgs:
        via = f" via {b['_found_via']}" if b.get("_found_via") else ""
        if b.get("_error"):
            lines.append(f"  - {b.get('rnb_id')}: ERROR {b['_error']}{via}")
            continue
        plots = b.get("plots") or []
        plot_str = ", ".join(
            f"{p.get('id')}({p.get('bdg_cover_ratio'):.2f})"
            if isinstance(p.get('bdg_cover_ratio'), (int, float)) else str(p.get('id'))
            for p in plots
        )
        exts = ", ".join(sorted({e.get("source", "?") for e in (b.get("ext_ids") or [])})) or "—"
        lines.append(
            f"  - {b.get('rnb_id')} [{b.get('status')}]{via} "
            f"addresses={len(b.get('addresses') or [])} "
            f"plots=[{plot_str}] ext_ids={exts}"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Extract everything the RNB knows about an address into JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("address", help="address to look up (quote it)")
    p.add_argument("-o", "--output", help="output JSON file "
                   "(default: rnb_<slug>.json in the current directory)")
    p.add_argument("--radius", type=int, default=60,
                   help="fallback 'closest' search radius in metres, 0-1000 "
                        "(used only if the address match is empty; default 60)")
    p.add_argument("--no-expand-plots", dest="expand_plots", action="store_false",
                   help="don't also fetch other buildings sharing the matched "
                        "cadastral plots (expansion is on by default and is what "
                        "finds a landlocked/back building on a divided plot)")
    p.add_argument("--from-email", default=None,
                   help="optional contact email sent to the RNB API (its `from` "
                        "param) so they can warn you of breaking API changes")
    p.add_argument("--pause", type=float, default=0.15,
                   help="seconds between requests (rate limit is 20/s; default 0.15)")
    p.add_argument("--print", dest="to_stdout", action="store_true",
                   help="also print the full JSON to stdout")
    args = p.parse_args(argv)

    if not 0 <= args.radius <= 1000:
        p.error("--radius must be between 0 and 1000 metres")

    try:
        bundle = lookup(args.address, args.radius, args.from_email, args.pause,
                        expand_plots=args.expand_plots)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 2

    out_path = args.output or f"rnb_{_slug(args.address)}.json"
    out_path = os.path.abspath(out_path)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=2, ensure_ascii=False)

    print(_summarize(bundle), file=sys.stderr)
    print(f"\nSaved -> {out_path}", file=sys.stderr)
    if args.to_stdout:
        print(json.dumps(bundle, indent=2, ensure_ascii=False))

    return 0 if bundle["buildings"] else 1


if __name__ == "__main__":
    sys.exit(main())
