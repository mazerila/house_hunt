"""Feature 2 — Cadastre. Parcel geometry/area from IGN apicarto.

Keyed off the RNB building plot `idu`s (falls back to a point lookup). An idu is
INSEE(5) + prefixe(3) + section(2) + numero(4), e.g. 920190000F0300.
"""
from __future__ import annotations

import asyncio

import httpclient as http, settings
from models import Section, error, ok


def parse_idu(idu: str) -> dict:
    return {
        "code_insee": idu[:5],
        "prefixe": idu[5:8],
        "section": idu[8:10],
        "numero": idu[10:14],
    }


async def _one(idu: str, cover_ratio: float | None):
    parts = parse_idu(idu)
    payload, meta = await http.get_json("cadastre", settings.CADASTRE_PARCELLE, {
        "code_insee": parts["code_insee"],
        "section": parts["section"],
        "numero": parts["numero"],
    })
    feats = payload.get("features") or []
    if not feats:
        return None, meta
    f = feats[0]
    p = f.get("properties") or {}
    return {
        "idu": p.get("idu", idu),
        "section": p.get("section"),
        "numero": p.get("numero"),
        "contenance_m2": p.get("contenance"),
        "commune": p.get("nom_com"),
        "cover_ratio": cover_ratio,
        "geometry": f.get("geometry"),
    }, meta


async def fetch_for_plots(plots: list[dict]) -> Section:
    """plots: [{idu, cover_ratio}] from the RNB building section."""
    if not plots:
        return error("no cadastral plots to resolve", skipped=True)

    results = await asyncio.gather(
        *(_one(p["idu"], p.get("cover_ratio")) for p in plots),
        return_exceptions=True,
    )
    parcels, urls = [], []
    failed = 0
    cached_all = True
    for res in results:
        if isinstance(res, Exception):
            failed += 1
            continue
        parcel, meta = res
        urls.append(meta["url"])
        cached_all = cached_all and meta["cached"]
        if parcel:
            parcels.append(parcel)

    if not parcels:
        return error("cadastre lookup returned no parcels",
                     url=settings.CADASTRE_PARCELLE)
    total = sum(p["contenance_m2"] for p in parcels if p.get("contenance_m2"))
    return ok({"parcels": parcels, "total_area_m2": total},
              urls=urls, cached=cached_all, partial=failed > 0)
