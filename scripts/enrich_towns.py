#!/usr/bin/env python3
"""Refresh the measured rows of the town sheets (towns/*.md).

The comparison tab is only as good as its inputs, and several Repères were
hand-typed from mixed sources ("Prix maison (2025)" from portal estimators, and
a terrain size the intro itself admitted was *indicative*). This script
recomputes the ones that can be measured, from open data, on the same basis for
every commune, and writes them back as ordinary Repères bullets — so the md
stays the source of truth and the dashboard needs no change at all.

Rows written (upserted, so re-running is idempotent):
    Prix maison DVF (2023-25)     geo-DVF, median €/m² of closed house sales
    Terrain médian (DVF)          geo-DVF, median plot actually sold
    Cambriolages (‰ logements)    SSMSI 2025, base communale de la délinquance
    Violences (‰ hab.)            SSMSI 2025, physiques hors cadre familial + vols violents
    Dégradations (‰ hab.)         SSMSI 2025, destructions et dégradations volontaires

Run:  python3 scripts/enrich_towns.py            # all sheets
      python3 scripts/enrich_towns.py --id chatou
      python3 scripts/enrich_towns.py --dry-run

Stdlib only. Public-record data about communes — no user data leaves the machine.
"""

from __future__ import annotations

import argparse
import csv
import glob
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOWNS_DIR = os.path.join(REPO_ROOT, "towns")
CACHE_DIR = os.path.join(REPO_ROOT, ".cache", "towns")
HISTORY_PATH = os.path.join(TOWNS_DIR, "price_history.json")
UA = {"User-Agent": "house-hunt/0.1"}

GEO = "https://geo.api.gouv.fr/communes"
DVF = "https://geo-dvf.s3.sbg.io.cloud.ovh.net/latest/csv/{y}/communes/{d}/{c}.csv"
CRIME_RID = "44ef4323-1097-48d5-8719-3c544b55d294"   # SSMSI base communale, data.gouv
CRIME = "https://tabular-api.data.gouv.fr/api/resources/" + CRIME_RID + "/data/"
YEARS = (2023, 2024, 2025)
# geo-DVF publishes 2021 onwards; earlier years 404. The comparison rows average
# the last three, the price history plots each one on its own.
HISTORY_YEARS = (2021, 2022, 2023, 2024, 2025)
# Below this many closed sales a yearly median says more about which houses
# happened to sell than about the market — plotted, but flagged as thin.
THIN_YEAR = 12
CRIME_YEAR = 2025

# Sheets that are not a whole commune: restrict DVF to the quartier's footprint.
# (Commune-level rows — density, income, crime — stay those of the commune.)
BBOX = {"porchefontaine": (2.145, 2.172, 48.790, 48.806)}


def cached(key, fetch, ttl_days=45, force=False):
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = os.path.join(CACHE_DIR, re.sub(r"[^a-zA-Z0-9_.-]", "_", key)[:170] + ".json")
    if not force and os.path.exists(p) and (time.time() - os.path.getmtime(p)) / 86400 < ttl_days:
        try:
            with open(p, encoding="utf-8") as f:
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


def get_json(url, timeout=45):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.load(r)


def fr(n, dec=0):
    """French formatting: 5067 -> '5 067', 6.01 -> '6,01'."""
    if dec:
        return f"{n:,.{dec}f}".replace(",", " ").replace(".", ",")
    return f"{round(n):,}".replace(",", " ")


def median(v):
    v = sorted(v)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


# --------------------------------------------------------------------------
def resolve(tid, title, force=False):
    """Sheet title -> commune record. Postcode disambiguates homonyms (a bare
    'Bailly' matches the Oise commune before the Yvelines one)."""
    m = re.search(r"\((\d{5})\)", title)
    cp = m.group(1) if m else ""
    name = title.split("(")[0].strip()
    name = re.sub(r"^.*—\s*", "", name)          # "Porchefontaine — Versailles" -> "Versailles"

    def _fetch():
        for params in ({"nom": name, "codePostal": cp}, {"codePostal": cp}, {"nom": name}):
            params.update({"fields": "nom,code,population,surface", "limit": 1})
            try:
                d = get_json(GEO + "?" + urllib.parse.urlencode(params), timeout=20)
            except Exception:
                continue
            if d:
                c = d[0]
                return {"insee": c["code"], "nom": c["nom"], "pop": c["population"],
                        "surface": c["surface"]}
        return None

    return cached(f"geo_{tid}", _fetch, force=force)


def dvf_stats(insee, box=None, force=False):
    def _fetch():
        agg = {}
        for y in YEARS:
            url = DVF.format(y=y, d=insee[:2], c=insee)
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as r:
                    txt = r.read().decode("utf-8", "replace")
            except Exception:
                continue
            for row in csv.DictReader(io.StringIO(txt)):
                if not row.get("id_parcelle") or row.get("type_local") != "Maison":
                    continue
                if box:
                    try:
                        lon, lat = float(row["longitude"]), float(row["latitude"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    if not (box[0] <= lon <= box[1] and box[2] <= lat <= box[3]):
                        continue
                a = agg.setdefault(row["id_mutation"], {"bati": 0.0, "terr": 0.0, "val": None})
                try:
                    a["bati"] += float(row["surface_reelle_bati"] or 0)
                    a["terr"] += float(row["surface_terrain"] or 0)
                    a["val"] = float(row["valeur_fonciere"] or 0)
                except ValueError:
                    pass
        pm = [a["val"] / a["bati"] for a in agg.values()
              if a["val"] and a["bati"] >= 60 and a["val"] > 150000]
        tr = [a["terr"] for a in agg.values()
              if a["terr"] > 0 and a["val"] and a["bati"] >= 60 and a["val"] > 150000]
        if len(pm) < 8:
            return None
        return {"n": len(pm), "med": round(median(pm)),
                "terr": round(median(tr)) if tr else None}

    return cached(f"dvf_{insee}_{'q' if box else 'c'}", _fetch, force=force)


def dvf_year_medians(insee, box=None, force=False):
    """Median €/m² of closed house sales, year by year — the price history.

    Same basis as dvf_stats (aggregate by id_mutation, drop tiny or symbolic
    sales) so the chart and the "Prix maison DVF" row can never disagree.
    """
    def _fetch():
        out = {}
        for y in HISTORY_YEARS:
            url = DVF.format(y=y, d=insee[:2], c=insee)
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as r:
                    txt = r.read().decode("utf-8", "replace")
            except Exception:
                continue
            agg = {}
            for row in csv.DictReader(io.StringIO(txt)):
                if not row.get("id_parcelle") or row.get("type_local") != "Maison":
                    continue
                if box:
                    try:
                        lon, lat = float(row["longitude"]), float(row["latitude"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    if not (box[0] <= lon <= box[1] and box[2] <= lat <= box[3]):
                        continue
                a = agg.setdefault(row["id_mutation"], {"bati": 0.0, "val": None})
                try:
                    a["bati"] += float(row["surface_reelle_bati"] or 0)
                    a["val"] = float(row["valeur_fonciere"] or 0)
                except ValueError:
                    pass
            pm = [a["val"] / a["bati"] for a in agg.values()
                  if a["val"] and a["bati"] >= 60 and a["val"] > 150000]
            if pm:
                out[str(y)] = {"med": round(median(pm)), "n": len(pm)}
        return out or None

    return cached(f"dvfhist_{insee}_{'q' if box else 'c'}", _fetch, force=force)


def crime_stats(insee, force=False):
    def _fetch():
        q = urllib.parse.urlencode({"CODGEO_2026__exact": insee,
                                    "annee__exact": str(CRIME_YEAR), "page_size": 40})
        try:
            d = get_json(CRIME + "?" + q)
        except Exception:
            return None
        ind = {}
        for r in d.get("data") or []:
            t = r.get("taux_pour_mille")
            if r.get("indicateur") and t not in (None, ""):
                try:
                    ind[r["indicateur"]] = float(t)
                except (TypeError, ValueError):
                    pass
        if not ind:
            return None
        viol = (ind.get("Violences physiques hors cadre familial") or 0) \
            + (ind.get("Vols violents sans arme") or 0)
        return {"camb": ind.get("Cambriolages de logement"),
                "viol": round(viol, 2),
                "degr": ind.get("Destructions et dégradations volontaires")}

    return cached(f"crime_{insee}", _fetch, force=force)


# --------------------------------------------------------------------------
# upsert Repères bullets, keeping related rows adjacent
INSERT_AFTER = {
    "Prix maison DVF (2023-25)": "Prix maison (2025)",
    "Terrain médian (DVF)": "Terrain maison (indicatif)",
    "Cambriolages (‰ logements)": "Logement social",
    "Violences (‰ hab.)": "Cambriolages (‰ logements)",
    "Dégradations (‰ hab.)": "Violences (‰ hab.)",
}
ORDER = ["Prix maison DVF (2023-25)", "Terrain médian (DVF)",
         "Cambriolages (‰ logements)", "Violences (‰ hab.)", "Dégradations (‰ hab.)"]


def upsert(body, key, value):
    line = f"- **{key}**: {value}"
    pat = re.compile(r"^- \*\*" + re.escape(key) + r"\*\*:.*$", re.M)
    if pat.search(body):
        return pat.sub(lambda m: line, body, count=1)
    anchor = INSERT_AFTER.get(key)
    if anchor:
        apat = re.compile(r"^- \*\*" + re.escape(anchor) + r"\*\*:.*$", re.M)
        m = apat.search(body)
        if m:
            return body[:m.end()] + "\n" + line + body[m.end():]
    # no anchor found → append at the end of the Repères section
    m = re.search(r"^## Repères\s*$", body, re.M)
    if not m:
        return body
    nxt = re.search(r"^## ", body[m.end():], re.M)
    cut = m.end() + (nxt.start() if nxt else len(body) - m.end())
    return body[:cut].rstrip() + "\n" + line + "\n\n" + body[cut:].lstrip("\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", help="only sheets whose filename contains this")
    ap.add_argument("--force", action="store_true", help="bypass the cache")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(TOWNS_DIR, "*.md")))
    if args.id:
        paths = [p for p in paths if args.id.lower() in os.path.basename(p).lower()]
    if not paths:
        sys.exit("no town sheets matched")

    changed = 0
    history = {}
    for path in paths:
        tid = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8") as f:
            body = f.read()
        title = body.splitlines()[0].lstrip("# ").strip()
        geo = resolve(tid, title, args.force)
        if not geo:
            print(f"{tid:24} — commune non résolue, ignoré")
            continue
        d = dvf_stats(geo["insee"], BBOX.get(tid), args.force)
        c = crime_stats(geo["insee"], args.force)
        hist = dvf_year_medians(geo["insee"], BBOX.get(tid), args.force)
        if hist:
            history[tid] = {"title": title, "insee": geo["insee"],
                            "quartier": tid in BBOX, "years": hist}

        vals = {}
        if d:
            suffix = " *(quartier)*" if tid in BBOX else ""
            vals["Prix maison DVF (2023-25)"] = f"**{fr(d['med'])} €/m²** (médiane, n={d['n']}){suffix}"
            if d["terr"]:
                vals["Terrain médian (DVF)"] = f"{fr(d['terr'])} m² (médiane des ventes){suffix}"
        if c:
            if c["camb"] is not None:
                vals["Cambriolages (‰ logements)"] = f"{fr(c['camb'], 2)} ‰"
            vals["Violences (‰ hab.)"] = f"{fr(c['viol'], 2)} ‰"
            if c["degr"] is not None:
                vals["Dégradations (‰ hab.)"] = f"{fr(c['degr'], 2)} ‰"

        new = body
        for k in ORDER:
            if k in vals:
                new = upsert(new, k, vals[k])
        if new != body:
            changed += 1
            if not args.dry_run:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new)
        flag = "" if new != body else " (inchangé)"
        print(f"{tid:24} {geo['insee']} "
              f"{(fr(d['med'])+' €/m²') if d else 'DVF n/a':>14} "
              f"{(fr(d['terr'])+' m²') if d and d['terr'] else '—':>9} "
              f"camb {fr(c['camb'],2) if c and c['camb'] is not None else '—':>6} "
              f"viol {fr(c['viol'],2) if c else '—':>5}{flag}")

    # The per-year series is a chart input, not a Repères row: it goes to a
    # tracked JSON the dashboard reads. Merged, so `--id` refreshes one commune
    # without dropping the others.
    if history and not args.dry_run:
        prev = {}
        if os.path.exists(HISTORY_PATH):
            try:
                with open(HISTORY_PATH, encoding="utf-8") as f:
                    prev = json.load(f).get("communes", {})
            except (json.JSONDecodeError, OSError):
                prev = {}
        prev.update(history)
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump({"years": list(HISTORY_YEARS), "thin_below": THIN_YEAR,
                       "generated": time.strftime("%Y-%m-%d"),
                       "communes": dict(sorted(prev.items()))}, f,
                      ensure_ascii=False, indent=1)
        print(f"série de prix écrite pour {len(history)} commune(s) -> {os.path.relpath(HISTORY_PATH, REPO_ROOT)}")

    print(f"\n{changed}/{len(paths)} fiches mises à jour" + (" (--dry-run)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
