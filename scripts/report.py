"""Orchestrate the 3-step listing investigation into one report.

  1+2  extract the reference listing (text -> summary -> JSON)
  3    find alternative listings (web search) and extract each
  --   reconcile fields across all of them (consensus + discrepancies)

Always starts from scratch — no cached results.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from listing import extract_one
from reconcile import reconcile
from search import find_alternatives

MAX_ALTERNATIVES = 2


async def build_report(url: str, max_alternatives: int = MAX_ALTERNATIVES) -> dict:
    reference = {**await extract_one(url), "portal": "reference"}

    alts_meta = await find_alternatives(reference.get("fields"), url, max_alternatives)
    alt_results = (
        await asyncio.gather(*(extract_one(c["url"]) for c in alts_meta))
        if alts_meta else []
    )
    alternatives = [
        {**res, "portal": meta.get("portal"), "why": meta.get("why")}
        for meta, res in zip(alts_meta, alt_results)
    ]

    listings = [reference, *alternatives]
    rec = reconcile([{"url": l["url"], "portal": l.get("portal"), "fields": l.get("fields")}
                     for l in listings])

    return {
        "query_url": url,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "reference": reference,
        "alternatives": alternatives,
        "merged": rec["merged"],
        "field_report": rec["field_report"],
        "discrepancies": rec["discrepancies"],
    }
