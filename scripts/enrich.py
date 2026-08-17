#!/usr/bin/env python3
"""Enrich the dashboard DB with the data that actually decides a listing.

Every dossier written by hand repeats the same four API checks (PLU zone,
prescriptions, servitudes, Géorisques) and the same arithmetic (all-in cost vs
post-works value). This script does them once per listing from the coordinates
already recorded in the note, and stores the result as *structured* fields so
the dashboard can badge, sort and filter on them instead of burying them in
prose.

Run:  ./.venv/bin/python scripts/enrich.py            # all listings
      ./.venv/bin/python scripts/enrich.py --id h10   # one listing
      ./.venv/bin/python scripts/enrich.py --force    # ignore the disk cache

Stdlib only (like dashboard.py) so it runs with any interpreter.

PRIVACY: commute anchors live in private/anchors.json (gitignored). Only their
coordinates leave this machine, and only to a routing API the user has opted
into with their own key. No name, e-mail or employer string is ever sent.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRIVATE_DIR = os.path.join(REPO_ROOT, "private")
DB_PATH = os.path.join(PRIVATE_DIR, "dashboard_db.json")
ANCHORS_PATH = os.path.join(PRIVATE_DIR, "anchors.json")
CACHE_DIR = os.path.join(REPO_ROOT, ".cache", "enrich")

UA = "house-hunt/0.1"
CONTACT = os.environ.get("HOUSE_HUNT_FROM_EMAIL", "")
if CONTACT:  # opt-in only, never hardcoded (see CLAUDE.md privacy rule)
    UA = f"house-hunt/0.1 ({CONTACT})"

GPU = "https://apicarto.ign.fr/api/gpu"
GEORISQUES = "https://georisques.gouv.fr/api/v1"
DVF_CSV = "https://geo-dvf.s3.sbg.io.cloud.ovh.net/latest/csv/{year}/communes/{dept}/{insee}.csv"
BAN = "https://api-adresse.data.gouv.fr/search/"

# Servitude prefixes that mean "an architect des Bâtiments de France signs off
# on anything you change outside" — the buyer's stated hard filter.
HERITAGE_SUP = {
    "ac1": "abords Monument Historique (ABF)",
    "ac2": "site inscrit / classé",
    "ac4": "Site patrimonial remarquable (SPR)",
}
# Prescription labels that cap what can be built. Matched case-insensitively on
# a substring, because the GPU wording varies by commune.
# NB: match on the ACCENT-STRIPPED label — the GPU spells the same prescription
# both ways ("Protection des lisieres en SUC" on Jouy, "lisières" elsewhere).
PRESCRIPTION_FLAGS = [
    ("espace boise classe", "red", "EBC — espace boisé classé"),
    ("lisiere", "amber", "lisière de massif boisé (bande 50 m)"),
    ("espace paysager", "red", "EPP — espace paysager à protéger"),
    ("emplacement reserve", "red", "emplacement réservé"),
    ("orientation d'amenagement", "amber", "OAP"),
    ("mixite", "amber", "secteur de mixité sociale"),
]


def _deaccent(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(c) != "Mn").lower()


# --------------------------------------------------------------------------
# tiny disk cache — these are slow, rate-limited, rarely-changing endpoints
# --------------------------------------------------------------------------
def _cache_path(key: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", key)[:180]
    return os.path.join(CACHE_DIR, safe + ".json")


def cached_get(key: str, fetch, force: bool = False, ttl_days: int = 30):
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = _cache_path(key)
    if not force and os.path.exists(p):
        age_days = (time.time() - os.path.getmtime(p)) / 86400
        if age_days < ttl_days:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                pass
    val = fetch()
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(val, f)
    except OSError:
        pass
    return val


def get_json(url: str, timeout: int = 40):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def get_text(url: str, timeout: int = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def point_geom(lon: float, lat: float) -> str:
    return urllib.parse.quote(json.dumps({"type": "Point", "coordinates": [lon, lat]}))


def haversine_km(a_lon, a_lat, b_lon, b_lat) -> float:
    R = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


# --------------------------------------------------------------------------
# extraction from the dossier text
# --------------------------------------------------------------------------
COORD_RE = re.compile(
    r"Coordonn[ée]es\s*[:\-]?\s*\*{0,2}\s*(-?\d+[.,]\d+)\s*(?:N)?\s*[,;/]\s*(-?\d+[.,]\d+)\s*(?:E)?",
    re.IGNORECASE,
)
INSEE_RE = re.compile(r"\b(\d{5})000[A-Z]{2}\d{4}\b")          # from a parcel idu
POSTCODE_RE = re.compile(r"\b(\d{5})\b")


def geocode(q: str, force=False) -> tuple[float, float] | None:
    """Property address -> (lon, lat). Public-record data about a property, not
    the user's own identity, so this is fine to send to the BAN (CLAUDE.md)."""
    def _fetch():
        url = BAN + "?" + urllib.parse.urlencode({"q": q, "limit": 1})
        try:
            d = get_json(url, timeout=20)
        except Exception:
            return None
        feats = d.get("features") or []
        if not feats:
            return None
        p, g = feats[0]["properties"], feats[0]["geometry"]["coordinates"]
        return {"lon": g[0], "lat": g[1], "score": p.get("score"), "label": p.get("label")}

    hit = cached_get("ban_" + q, _fetch, force, ttl_days=180)
    if not hit or (hit.get("score") or 0) < 0.4:
        return None
    return hit["lon"], hit["lat"]


def extract_coords(rec) -> tuple[float, float] | None:
    """(lon, lat) from the note's `Coordonnées:` line, else geocode its H1 title.

    Older dossiers pre-date the `Coordonnées:` convention, so fall back to the
    address in the title rather than skipping the listing entirely."""
    body = rec.get("md_body") or ""
    m = COORD_RE.search(body)
    if m:
        lat = float(m.group(1).replace(",", "."))
        lon = float(m.group(2).replace(",", "."))
        if 41 < lat < 52 and -6 < lon < 10:  # France métropolitaine
            return lon, lat
    title = (rec.get("title") or "").strip()
    if title:
        # titles carry trailing commentary after an em-dash ("… — maison 1971 (…)")
        addr = re.split(r"\s+[—–]\s+", title)[0].strip()
        return geocode(addr)
    return None


def extract_insee(rec) -> str | None:
    body = rec.get("md_body") or ""
    m = INSEE_RE.search(body)
    if m:
        return m.group(1)
    return None


def reverse_insee(lon: float, lat: float) -> str | None:
    url = "https://api-adresse.data.gouv.fr/reverse/?" + urllib.parse.urlencode(
        {"lon": lon, "lat": lat, "limit": 1}
    )
    try:
        d = get_json(url, timeout=20)
        feats = d.get("features") or []
        if feats:
            return feats[0]["properties"].get("citycode")
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------
# the red-flag screen
# --------------------------------------------------------------------------
def screen_parcel(lon: float, lat: float, force=False) -> dict:
    geom = point_geom(lon, lat)
    key = f"{lat:.6f}_{lon:.6f}"

    def _zone():
        try:
            d = get_json(f"{GPU}/zone-urba?geom={geom}")
            return [f["properties"] for f in d.get("features", [])]
        except Exception:
            return []

    def _presc():
        try:
            d = get_json(f"{GPU}/prescription-surf?geom={geom}")
            return [f["properties"] for f in d.get("features", [])]
        except Exception:
            return []

    def _sup():
        try:
            d = get_json(f"{GPU}/assiette-sup-s?geom={geom}")
            return [f["properties"] for f in d.get("features", [])]
        except Exception:
            return []

    def _rga():
        try:
            return get_json(f"{GEORISQUES}/rga?latlon={lon}%2C{lat}")
        except Exception:
            return {}

    def _cav():
        try:
            d = get_json(f"{GEORISQUES}/cavites?latlon={lon}%2C{lat}&rayon=1000&page=1&page_size=20")
            return len(d.get("data") or [])
        except Exception:
            return None

    zones = cached_get(f"zone_{key}", _zone, force)
    prescs = cached_get(f"presc_{key}", _presc, force)
    sups = cached_get(f"sup_{key}", _sup, force)
    rga = cached_get(f"rga_{key}", _rga, force)
    cavites = cached_get(f"cav_{key}", _cav, force)

    flags = []
    heritage = []
    for s in sups:
        st = (s.get("suptype") or "").lower()
        name = s.get("nomsuplitt") or ""
        if st in HERITAGE_SUP:
            heritage.append({"type": st.upper(), "name": name})
            flags.append({"level": "red", "code": st.upper(),
                          "label": f"{HERITAGE_SUP[st]}" + (f" — {name}" if name else "")})
        elif st == "pm1":
            flags.append({"level": "red", "code": "PM1", "label": f"carrières (PM1) — {name}"})

    presc_labels = []
    for p in prescs:
        lab = (p.get("libelle") or "").strip()
        if lab:
            presc_labels.append(lab)
        low = _deaccent(lab)
        for needle, level, human in PRESCRIPTION_FLAGS:
            if needle in low:
                flags.append({"level": level, "code": "PRESC", "label": human})
                break

    rga_code = str(rga.get("codeExposition") or "")
    if rga_code == "3":
        flags.append({"level": "amber", "code": "RGA", "label": "argile — exposition FORTE"})
    elif rga_code == "2":
        flags.append({"level": "amber", "code": "RGA", "label": "argile — exposition moyenne"})

    if cavites:
        flags.append({"level": "amber", "code": "CAV", "label": f"{cavites} cavité(s) < 1 km"})

    # de-duplicate (same label can come from several features)
    seen, uniq = set(), []
    for f in flags:
        k = (f["code"], f["label"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)

    if not uniq:
        uniq.append({"level": "green", "code": "OK", "label": "aucun drapeau détecté"})

    return {
        "zone": ", ".join(sorted({z.get("libelle") for z in zones if z.get("libelle")})) or None,
        "prescriptions": presc_labels,
        "servitudes": [{"type": (s.get("suptype") or "").upper(), "name": s.get("nomsuplitt")}
                       for s in sups],
        "rga": {"code": rga_code, "label": rga.get("exposition")},
        "cavites_1km": cavites,
        "flags": uniq,
        "heritage_locked": bool(heritage),
        "heritage": heritage,
    }


# --------------------------------------------------------------------------
# DVF commune median (feeds the all-in spread)
# --------------------------------------------------------------------------
def commune_median_m2(insee: str, years=(2023, 2024, 2025), force=False) -> dict | None:
    if not insee or len(insee) != 5:
        return None
    dept = insee[:2]

    def _fetch():
        rows = []
        for y in years:
            url = DVF_CSV.format(year=y, dept=dept, insee=insee)
            try:
                txt = get_text(url)
            except Exception:
                continue
            for r in csv.DictReader(io.StringIO(txt)):
                if r.get("id_parcelle") and r.get("type_local") == "Maison":
                    rows.append(r)
        agg = {}
        for r in rows:
            k = r["id_mutation"]
            a = agg.setdefault(k, {"bati": 0.0, "val": None})
            try:
                a["bati"] += float(r["surface_reelle_bati"] or 0)
                a["val"] = float(r["valeur_fonciere"] or 0)
            except ValueError:
                pass
        pm = sorted(a["val"] / a["bati"] for a in agg.values()
                    if a["val"] and a["bati"] >= 60 and a["val"] > 150000)
        if not pm:
            return None
        n = len(pm)
        med = pm[n // 2] if n % 2 else (pm[n // 2 - 1] + pm[n // 2]) / 2
        # p75 is the realistic "renovated, top of range" resale figure
        p75 = pm[min(n - 1, int(n * 0.75))]
        return {"insee": insee, "n": n, "median_m2": round(med), "p75_m2": round(p75)}

    return cached_get(f"dvf_{insee}", _fetch, force, ttl_days=60)


# --------------------------------------------------------------------------
# commute
# --------------------------------------------------------------------------
def load_anchors() -> list:
    if not os.path.exists(ANCHORS_PATH):
        return []
    try:
        with open(ANCHORS_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("anchors") or []
    except (OSError, json.JSONDecodeError):
        return []


def navitia_minutes(from_lon, from_lat, to_lon, to_lat, key, force=False):
    """Door-to-door public-transport minutes, if the user opted in with a key."""
    if not key:
        return None
    ck = f"nav_{from_lat:.5f}_{from_lon:.5f}_{to_lat:.5f}_{to_lon:.5f}"

    def _fetch():
        base = "https://api.navitia.io/v1/coverage/fr-idf/journeys"
        q = urllib.parse.urlencode({
            "from": f"{from_lon};{from_lat}",
            "to": f"{to_lon};{to_lat}",
            "datetime": time.strftime("%Y%m%dT080000", time.localtime(
                time.time() + 86400 * ((7 - time.localtime().tm_wday) % 7 or 7))),
            "datetime_represents": "arrival",
            "max_nb_journeys": 3,
        })
        req = urllib.request.Request(base + "?" + q,
                                     headers={"User-Agent": UA, "Authorization": key})
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.load(r)
        durations = [j.get("duration") for j in d.get("journeys", []) if j.get("duration")]
        return round(min(durations) / 60) if durations else None

    try:
        return cached_get(ck, _fetch, force, ttl_days=90)
    except Exception:
        return None


def commute_for(lon, lat, anchors, key, force=False) -> dict:
    out = {}
    for a in anchors:
        km = round(haversine_km(lon, lat, a["lon"], a["lat"]), 1)
        out[a["id"]] = {
            "label": a.get("label") or a["id"],
            "km_crow": km,
            "transit_min": navitia_minutes(lon, lat, a["lon"], a["lat"], key, force),
        }
    return out


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", help="only this listing id (substring match, e.g. 'jouy')")
    ap.add_argument("--force", action="store_true", help="bypass the disk cache")
    ap.add_argument("--dry-run", action="store_true", help="print, do not write the db")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        sys.exit(f"no db at {DB_PATH} — run the dashboard and Réimporter first")
    with open(DB_PATH, "r", encoding="utf-8") as f:
        db = json.load(f)

    anchors = load_anchors()
    nav_key = os.environ.get("HOUSE_HUNT_NAVITIA_KEY", "")
    if anchors and not nav_key:
        print("note: no HOUSE_HUNT_NAVITIA_KEY in .env — commute = crow-fly km only\n"
              "      (free key: https://navitia.io/ → add HOUSE_HUNT_NAVITIA_KEY=... to .env)\n")

    listings = db.get("listings", {})
    todo = [(lid, rec) for lid, rec in listings.items()
            if not args.id or args.id.lower() in lid.lower()
            or args.id.lower() in (rec.get("code") or "").lower()]
    todo.sort(key=lambda kv: kv[1].get("code") or "")

    for lid, rec in todo:
        code = rec.get("code") or lid
        coords = extract_coords(rec)
        if not coords:
            print(f"{code:5} {lid:26} — pas de `Coordonnées:` dans la note, ignoré")
            continue
        lon, lat = coords
        insee = extract_insee(rec) or reverse_insee(lon, lat)

        screen = screen_parcel(lon, lat, args.force)
        dvf = commune_median_m2(insee, force=args.force) if insee else None
        comm = commute_for(lon, lat, anchors, nav_key, args.force) if anchors else {}

        enr = {
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "coords": {"lat": lat, "lon": lon},
            "insee": insee,
            **screen,
            "dvf": dvf,
            "commute": comm,
        }
        rec["enrichment"] = enr

        reds = [f for f in enr["flags"] if f["level"] == "red"]
        ambers = [f for f in enr["flags"] if f["level"] == "amber"]
        badge = "🔴" * len(reds) + "🟠" * len(ambers) or "🟢"
        med = f"{dvf['median_m2']:>5} €/m² (n={dvf['n']})" if dvf else "  n/a"
        print(f"{code:5} {lid:26} {badge:6} zone {str(enr['zone'] or '?'):10} "
              f"DVF {med}" + (f"  {'/'.join(str(c['km_crow']) for c in comm.values())} km" if comm else ""))
        for f in reds + ambers:
            print(f"        {'🔴' if f['level']=='red' else '🟠'} {f['label']}")

    if args.dry_run:
        print("\n--dry-run: db not written")
        return
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB_PATH)
    print(f"\nwrote {DB_PATH}")


if __name__ == "__main__":
    main()
