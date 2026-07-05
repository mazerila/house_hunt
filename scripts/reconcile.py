"""Reconcile structured fields across the reference + alternative listings.

Rule-based and transparent: for each field, gather every non-null value with its
source, pick a consensus (most common; ties favor the reference, which is first),
and flag fields where listings disagree or where an alternative fills a gap the
reference left blank.
"""
from __future__ import annotations

from collections import Counter

# (key, label, kind)
FIELDS: list[tuple[str, str, str]] = [
    ("address", "Address", "str"),
    ("property_type", "Type", "str"),
    ("asking_price", "Asking price", "num"),
    ("price_per_m2", "€/m²", "num"),
    ("surface_m2", "Surface (m²)", "num"),
    ("rooms", "Rooms", "num"),
    ("bedrooms", "Bedrooms", "num"),
    ("dpe_class", "DPE class", "str"),
    ("energy_value", "DPE value", "num"),
    ("agency", "Agency", "str"),
]


def _norm(key: str, val, kind: str):
    if val is None or val == "":
        return None
    if kind == "num":
        try:
            return float(val)
        except (TypeError, ValueError):
            return None
    s = str(val).strip().lower()
    return s.upper() if key == "dpe_class" else s


def reconcile(listings: list[dict]) -> dict:
    """listings: [{url, portal, fields(dict|None)}] with the reference first."""
    usable = [l for l in listings if l.get("fields")]
    ref = usable[0] if usable else None

    field_report, discrepancies, merged = [], [], {}
    for key, label, kind in FIELDS:
        values = []
        for l in usable:
            raw = l["fields"].get(key)
            nv = _norm(key, raw, kind)
            if nv is not None:
                values.append({"value": raw, "norm": nv,
                               "source": l["url"], "portal": l.get("portal")})

        distinct = {v["norm"] for v in values}
        agree = len(distinct) <= 1

        consensus = None
        if values:
            top = Counter(v["norm"] for v in values).most_common(1)[0][0]
            consensus = next(v["value"] for v in values if v["norm"] == top)
        merged[key] = consensus

        ref_norm = _norm(key, ref["fields"].get(key), kind) if ref else None
        field_report.append({
            "key": key,
            "label": label,
            "values": [{"value": v["value"], "source": v["source"], "portal": v["portal"]}
                       for v in values],
            "agree": agree,
            "distinct_count": len(distinct),
            "filled_from_alt": ref_norm is None and bool(values),
        })
        if not agree:
            discrepancies.append(key)

    return {"merged": merged, "field_report": field_report, "discrepancies": discrepancies}
