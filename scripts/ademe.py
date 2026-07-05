"""Feature 4 — DPE. ADEME "logements existants" datafair API.

Primary lookup is the exact RNB-id join (qs=id_rnb:"…"), which ties multiple
DPEs for one building together — this is how the Sangnier E-vs-G conflict is
detected. Falls back to a geo_distance search when no RNB id is available.
"""
from __future__ import annotations

import asyncio

import httpclient as http, settings
from models import Section, error, ok

F = settings.ADEME_FIELDS


def _normalize(row: dict) -> dict:
    out = {our: row.get(src) for our, src in F.items()}
    # numeric coercion for the headline figures
    for k in ("conso_ep_m2", "emission_ges_m2", "surface_habitable",
              "annee_construction"):
        v = out.get(k)
        if v not in (None, ""):
            try:
                out[k] = float(v) if "." in str(v) else int(float(v))
            except (TypeError, ValueError):
                pass
    return out


def _select() -> str:
    return ",".join(sorted(set(F.values())))


async def _by_rnb(rnb_id: str):
    return await http.get_json("ademe", settings.ADEME_LINES, {
        "qs": f'id_rnb:"{rnb_id}"', "size": 20, "select": _select(),
        "sort": "-date_etablissement_dpe"})


async def _by_geo(lat: float, lon: float, radius_m: int):
    return await http.get_json("ademe", settings.ADEME_LINES, {
        "geo_distance": f"{lon},{lat},{radius_m}m", "size": 50,
        "select": _select(), "sort": "-date_etablissement_dpe"})


def _conflict(records: list[dict]) -> dict:
    classes = {r.get("etiquette_dpe") for r in records if r.get("etiquette_dpe")}
    detected = len(records) > 1 and len(classes) > 1
    note = None
    if detected:
        note = (f"{len(records)} DPEs for this building disagree "
                f"(classes {', '.join(sorted(classes))}).")
    return {"detected": detected,
            "fields": ["etiquette_dpe", "conso_ep_m2", "emission_ges_m2"] if detected else [],
            "note": note}


async def fetch(rnb_ids: list[str], lat: float | None, lon: float | None,
                radius_m: int = 150) -> Section:
    records: list[dict] = []
    urls: list[str] = []
    cached_all = True
    by = "rnb_id"

    if rnb_ids:
        results = await asyncio.gather(*(_by_rnb(r) for r in rnb_ids),
                                       return_exceptions=True)
        seen = set()
        for res in results:
            if isinstance(res, Exception):
                continue
            payload, meta = res
            urls.append(meta["url"])
            cached_all = cached_all and meta["cached"]
            for row in payload.get("results", []):
                num = row.get("numero_dpe")
                if num and num not in seen:
                    seen.add(num)
                    records.append(_normalize(row))

    if not records and lat is not None and lon is not None:
        by = "geo_distance"
        try:
            payload, meta = await _by_geo(lat, lon, radius_m)
            urls.append(meta["url"])
            cached_all = meta["cached"]
            records = [_normalize(r) for r in payload.get("results", [])]
        except Exception as e:  # noqa: BLE001
            return error(f"ADEME request failed: {e}", url=settings.ADEME_LINES)

    if not records:
        return ok({"records": [], "matched_by": by,
                   "conflict": {"detected": False, "fields": [], "note": None}},
                  urls=urls, cached=cached_all)

    return ok({"records": records, "matched_by": by,
               "conflict": _conflict(records)},
              urls=urls, cached=cached_all)
