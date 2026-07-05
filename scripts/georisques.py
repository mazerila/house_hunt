"""Feature 6 — Risk. Géorisques: clay (RGA), seismic, radon, CatNat history.

rga/zonage_sismique return flat dicts; radon/gaspar return paginated {data:[...]}.
Each sub-call can fail independently -> the section degrades to 'partial'.
"""
from __future__ import annotations

import asyncio

import httpclient as http, settings
from models import Section, error, ok

_DROUGHT = ("sécheresse", "secheresse", "réhydratation", "rehydratation", "argile")
_FLOOD = ("inondation", "coulée", "coulee")


async def _get(path: str, params: dict):
    return await http.get_json("georisques", f"{settings.GEORISQUES_BASE}/{path}", params)


def _first(data) -> dict:
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"][0] if data["data"] else {}
    return data if isinstance(data, dict) else {}


async def fetch(lat: float, lon: float, insee: str | None) -> Section:
    latlon = f"{lon},{lat}"
    calls = {
        "clay": _get("rga", {"latlon": latlon}),
        "seismic": _get("zonage_sismique", {"code_insee": insee}) if insee else None,
        "radon": _get("radon", {"code_insee": insee}) if insee else None,
        "catnat": _get("gaspar/catnat", {"code_insee": insee, "page_size": 50}) if insee else None,
    }
    keys = [k for k, v in calls.items() if v is not None]
    results = await asyncio.gather(*(calls[k] for k in keys), return_exceptions=True)

    data: dict = {}
    urls: list[str] = []
    failed: list[str] = []
    cached_all = True
    for key, res in zip(keys, results):
        if isinstance(res, Exception):
            failed.append(key)
            continue
        payload, meta = res
        urls.append(meta["url"])
        cached_all = cached_all and meta["cached"]
        if key == "clay":
            d = _first(payload)
            data["clay"] = {"exposure_code": d.get("codeExposition"),
                            "exposure_label": d.get("exposition")}
        elif key == "seismic":
            d = _first(payload)
            data["seismic"] = {"zone": d.get("code_zone"),
                               "label": d.get("zone_sismicite")}
        elif key == "radon":
            d = _first(payload)
            data["radon"] = {"class": d.get("classe_potentiel")}
        elif key == "catnat":
            items = (payload.get("data") if isinstance(payload, dict) else None) or []
            catnat = [{
                "risk": it.get("libelle_risque_jo"),
                "start": it.get("date_debut_evt"),
                "end": it.get("date_fin_evt"),
                "arrete_date": it.get("date_publication_arrete"),
            } for it in items]
            data["catnat"] = catnat
            low = [(c["risk"] or "").lower() for c in catnat]
            data["catnat_summary"] = {
                "total": len(catnat),
                "drought_count": sum(any(t in r for t in _DROUGHT) for r in low),
                "flood_count": sum(any(t in r for t in _FLOOD) for r in low),
            }

    if not data:
        return error("Géorisques unavailable", url=settings.GEORISQUES_BASE)
    return ok(data, urls=urls, cached=cached_all, partial=bool(failed))
