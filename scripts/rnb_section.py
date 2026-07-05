"""Feature 3 — Building. Async adapter around the (stdlib) rnb.lookup pipeline.

Kept separate from rnb.py so the CLI stays dependency-free; this module bridges
the sync RNB fan-out (address -> closest -> plot expansion) into a Section,
running it off the event loop via asyncio.to_thread.
"""
from __future__ import annotations

import asyncio

import settings
from models import Section, error, ok
import rnb


def _normalize(b: dict) -> dict:
    return {
        "rnb_id": b.get("rnb_id"),
        "rnb_status": b.get("status"),
        "is_active": b.get("is_active"),
        "point": b.get("point"),
        "footprint": b.get("shape"),
        "addresses": b.get("addresses") or [],
        "plots": [{"idu": p.get("id"), "cover_ratio": p.get("bdg_cover_ratio")}
                  for p in (b.get("plots") or [])],
        "ext_ids": b.get("ext_ids") or [],
        "found_via": b.get("_found_via"),
        "error": b.get("_error"),
    }


async def fetch(address: str, radius: int = 60) -> Section:
    try:
        bundle = await asyncio.to_thread(
            rnb.lookup, address, radius, settings.CONTACT_EMAIL, 0.1, True)
    except Exception as e:  # noqa: BLE001
        return error(f"RNB lookup failed: {e}", url=settings.RNB_BASE)

    buildings = [_normalize(b) for b in (bundle.get("buildings") or [])]
    if not buildings:
        return error("no RNB building found for this address", url=settings.RNB_BASE)
    return ok({"buildings": buildings}, url=settings.RNB_BASE)
