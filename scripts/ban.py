"""Feature 1 — Locate. Geocode an address via the BAN (Base Adresse Nationale)."""
from __future__ import annotations

import httpclient as http, settings
from models import Section, error, ok


async def geocode(address: str) -> Section:
    try:
        data, meta = await http.get_json("ban", settings.BAN_SEARCH, {
            "q": address, "limit": 1, "autocomplete": 0})
    except Exception as e:  # noqa: BLE001 - never raise to the aggregator
        return error(f"BAN request failed: {e}", url=settings.BAN_SEARCH)

    feats = data.get("features") or []
    if not feats:
        return error("no geocoding result", url=meta["url"])
    f = feats[0]
    lon, lat = (f.get("geometry") or {}).get("coordinates", [None, None])
    p = f.get("properties") or {}
    return ok({
        "label": p.get("label"),
        "score": p.get("score"),
        "ban_id": p.get("id"),
        "type": p.get("type"),
        "insee": p.get("citycode"),
        "postcode": p.get("postcode"),
        "city": p.get("city"),
        "lat": lat,
        "lon": lon,
    }, url=meta["url"], cached=meta["cached"])
