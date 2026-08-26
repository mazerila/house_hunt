#!/usr/bin/env python3
"""House Hunt dashboard: stdlib-only http.server app.

Serves a single-page dashboard (scripts/dashboard.html) backed by a JSON
"database" imported from private/listings/*.md markdown files.

Usage:
    python3 scripts/dashboard.py [--port 8420]

Binds to 127.0.0.1 only. Never sends any data outward — this is a purely
local tool over the user's own private/ notes.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import unicodedata
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
PRIVATE_DIR = os.path.join(REPO_ROOT, "private")
LISTINGS_DIR = os.path.join(PRIVATE_DIR, "listings")
# Target-towns comparison data is generic public-record info (no user PII) → it
# lives OUTSIDE private/ so it can be committed/shared, unlike the listings.
TOWNS_DIR = os.path.join(REPO_ROOT, "towns")
DB_PATH = os.path.join(PRIVATE_DIR, "dashboard_db.json")
HTML_PATH = os.path.join(SCRIPT_DIR, "dashboard.html")

# "archived" is a real status, but it doubles as a visibility switch: the grid
# hides archived listings unless the user flips to the archive view. Archiving
# stashes the previous status in "prev_status" so un-archiving restores it
# instead of forcing the user to remember whether the house was visited/rejected.
STATUS_VALUES = {"researching", "visit-planned", "visited", "offer", "rejected", "archived"}
USER_OWNED_FIELDS = {"rating", "status", "verdict", "tags", "comment", "mitoyennete",
                     "criteria_state"}
# Numeric fields derived from the md but that the user may correct by hand; once
# edited they are recorded in the record's "overrides" list and re-imports no
# longer touch them.
OVERRIDABLE_FIELDS = {"price", "surface", "land_surface"}
# Text fields seeded from the announce (Identity bullets) but that the user may
# fill/correct by hand when the listing doesn't carry them; edits are recorded in
# "overrides" so re-imports no longer touch them (same contract as the numerics).
OVERRIDABLE_STR_FIELDS = {"agency", "contact"}
# Pure user-set numeric negotiation fields (never derived from the md): the
# minimum price the seller might accept and the offer the buyer is willing to make.
# value_m2_post overrides the DVF p75 used for the post-works value: the commune
# p75 is too generous for a defect-carrying property (steep slope, unfinished
# works) or where a commune nouvelle drags in a pricier parent commune's sales.
USER_NUMERIC_FIELDS = {"price_min", "price_offer", "value_m2_post"}
# Key dates, user-owned and never derived from the md: "visits" is the ordered
# list of visit dates (1st, 2nd, ...) and "offer_date" the day the offer went in.
# Both are plain ISO "YYYY-MM-DD" strings so they sort as text.
USER_DATE_FIELDS = {"offer_date"}
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def clean_dates(values):
    """Keep well-formed ISO dates, de-duplicate, and sort so index 0 = 1st visit."""
    seen, out = set(), []
    for v in values or []:
        if not isinstance(v, str):
            continue
        v = v.strip()
        if not ISO_DATE_RE.match(v) or v in seen:
            continue
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            continue
        seen.add(v)
        out.append(v)
    return sorted(out)

# Column-picker: every column the grid can show (the frontend holds the labels &
# renderers; the server only validates keys and persists the chosen visible set,
# so the choice is shared across everyone hitting this server — "all users").
ALLOWED_COLUMNS = [
    "code", "title", "commune", "type", "mitoyennete", "price", "price_min", "price_offer", "price_per_m2",
    "works_total", "land_surface", "surface", "dpe", "rating", "status", "verdict", "tags",
    "visits", "offer_date", "agency", "contact", "created", "updated",
    # computed by scripts/enrich.py (red-flag screen, DVF, commute) + spread
    "flags", "zone", "spread", "commute_a", "commute_b",
]
DEFAULT_COLUMNS = [
    "code", "title", "commune", "flags", "price", "price_offer", "spread", "works_total", "land_surface", "surface",
    "dpe", "rating", "status", "visits", "offer_date", "updated",
]

# Frais de notaire, à la charge de l'acheteur: 8% du prix du bien (les travaux
# n'y sont pas soumis). Used for the all-in figure behind the spread column.
NOTAIRE_RATE = 0.08


def computed_spread(rec):
    """all-in cost vs realistic post-works resale value.

    all-in  = prix + frais de notaire + travaux estimés
    valeur  = surface habitable x DVF p75 €/m² of the commune (p75 = the
              renovated top-of-range, not the median of everything sold)
    The price side is the buyer's own offer (`price_offer`) whenever one is
    set — that is the number the decision actually turns on — and falls back
    to the asking price until an offer exists.
    Returns None unless a price, surface and the DVF read are all available, so
    the column stays empty rather than showing a made-up number.
    """
    enr = rec.get("enrichment") or {}
    dvf = enr.get("dvf") or {}
    price = rec.get("price_offer") or rec.get("price")
    price_basis = "offre" if rec.get("price_offer") else "prix demandé"
    surface = rec.get("surface")
    p75 = rec.get("value_m2_post") or dvf.get("p75_m2")
    if not (price and surface and p75):
        return None
    works = rec.get("works_total")
    if not works:
        # Without a works figure the "spread" is just price-vs-value, which on a
        # house needing 200 k€ of work reads as a big green number and is flatly
        # misleading. Report the gap as unresolved instead of guessing.
        return {"spread": None, "blocked": "works", "value_m2": p75,
                "value_post": round(surface * p75),
                "notaire_rate": NOTAIRE_RATE,
                "price_used": price, "price_basis": price_basis,
                "all_in_ex_works": round(price * (1 + NOTAIRE_RATE))}
    all_in = price * (1 + NOTAIRE_RATE) + works
    value = surface * p75
    return {
        "all_in": round(all_in),
        "value_post": round(value),
        "spread": round(value - all_in),
        "value_m2": p75,
        "value_m2_source": "manuel" if rec.get("value_m2_post") else "DVF p75 commune",
        "notaire_rate": NOTAIRE_RATE,
        "price_used": price,
        "price_basis": price_basis,
        "works_used": works,
        "works_known": True,
    }


def decorate(rec):
    """Record + the derived fields the grid needs (never persisted)."""
    out = dict(rec)
    enr = rec.get("enrichment") or {}
    out["flags"] = enr.get("flags") or []
    out["zone"] = enr.get("zone")
    out["heritage_locked"] = enr.get("heritage_locked")
    out["spread"] = computed_spread(rec)
    comm = enr.get("commute") or {}
    for key, slot in (("work-a", "commute_a"), ("work-b", "commute_b")):
        c = comm.get(key)
        out[slot] = {"min": c.get("transit_min"), "km": c.get("km_crow"),
                     "label": c.get("label")} if c else None
    return out


def works_total(works):
    """Sum of numeric line-item costs; None if there are no priced items."""
    total, seen = 0, False
    for item in works or []:
        c = item.get("cost") if isinstance(item, dict) else None
        if isinstance(c, (int, float)):
            total += c
            seen = True
    return total if seen else None

# Media gallery: photos & videos live in the listing's own folder
# private/listings/<id>/ (and an optional <id>/photos/ subfolder).
IMAGE_EXTS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
              ".webp": "image/webp", ".gif": "image/gif",
              ".heic": "image/heic", ".heif": "image/heif"}
VIDEO_EXTS = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
              ".m4v": "video/x-m4v", ".ogv": "video/ogg"}
# HEIC rarely renders in browsers → transcode to JPEG on the fly (macOS `sips`).
HEIC_EXTS = {".heic", ".heif"}
MEDIA_EXTS = dict(IMAGE_EXTS, **VIDEO_EXTS)
# Source documents (DDT, DPE, Carrez, ebook agence…) sit next to the photos in
# private/listings/<id>/ (and an optional <id>/docs/ subfolder).
DOC_EXTS = {".pdf": "application/pdf"}
# Sellers' filenames are opaque ("DDT_-_LDI-26-3982-BARBONNE.pdf", "Surface
# BARBONNE.pdf"), so the drawer shows a readable French title derived from
# keywords in the name. First match wins → order matters (the more specific
# document types come before the generic ones they contain).
DOC_TITLE_RULES = [
    (("audit energetique", "audit-energetique", "audit ener", "audit"), "Audit énergétique"),
    (("ddt", "dossier de diagnostic", "diagnostic technique", "diagnostics"), "Dossier de diagnostic technique (DDT)"),
    (("carrez", "superficie", "mesurage", "surface"), "Certificat de surface (loi Carrez)"),
    (("dpe", "performance energetique"), "Diagnostic de performance énergétique (DPE)"),
    (("amiante",), "Diagnostic amiante"),
    (("crep", "plomb"), "Constat de risque d'exposition au plomb (CREP)"),
    (("termite", "parasitaire", "merule"), "État parasitaire (termites)"),
    (("electri", "electr", "elec"), "État de l'installation électrique"),
    (("assainissement", "tout a l egout"), "Contrôle assainissement"),
    (("gaz",), "État de l'installation gaz"),
    (("erp", "etat des risques", "risques", "georisque"), "État des risques (ERP)"),
    (("taxe fonc", "tax fonc", "foncier", "fonciere"), "Taxe foncière"),
    (("taxe habitation", "habitation"), "Taxe d'habitation"),
    (("compromis", "promesse de vente", "avant contrat"), "Compromis / promesse de vente"),
    (("titre de propriete", "acte de vente", "acte authentique"), "Titre de propriété"),
    (("reglement de copro", "copropriete", "copro", "pv ag", "assemblee generale", "charges"),
     "Copropriété (règlement / PV / charges)"),
    (("devis", "chiffrage"), "Devis travaux"),
    (("geotech", "etude de sol", "g2 avp", "g2"), "Étude géotechnique"),
    (("permis", "declaration prealable", "pc ", "urbanisme", "cub", "certificat d urbanisme"),
     "Urbanisme (permis / DP / CU)"),
    (("cadastr", "plan", "releve"), "Plan / cadastre"),
    (("ebook", "brochure", "plaquette", "annonce", "mandat", "fiche"), "Ebook agence / annonce"),
    (("mesure", "mesures"), "Mesurage des surfaces"),
    (("photos", "photo", "projection"), "Photos / projections"),
]


def _deaccent(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def doc_title(name):
    """Short human title for a document filename, or None when nothing matches
    (the UI then falls back to the filename itself)."""
    stem = os.path.splitext(os.path.basename(name))[0]
    # normalise separators so "tax-foncier-2025" and "taxe_fonciere" both match
    hay = " " + re.sub(r"[^a-z0-9]+", " ", _deaccent(stem).lower()).strip() + " "
    for keys, title in DOC_TITLE_RULES:
        if any((" " + k.strip() + " ") in hay or k.strip() in hay for k in keys):
            year = re.search(r"\b(19|20)\d{2}\b", stem)
            # a year is meaningful on fiscal/annual documents, noise elsewhere
            if year and ("Taxe" in title or "Copropriété" in title):
                return title + " " + year.group(0)
            return title
    return None
PHOTO_CACHE = os.path.join(PRIVATE_DIR, ".media_cache")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def photo_dirs(listing_id):
    base = os.path.join(LISTINGS_DIR, listing_id)
    return [base, os.path.join(base, "photos"), os.path.join(base, "plans")]


# Floor plans, plan de masse, cadastral extracts: they answer different
# questions from the photos, so the drawer shows them in their own section.
# Two ways in, and the folder is the reliable one: anything under
# <slug>/plans/ is a plan by placement, anything elsewhere by its filename.
# The name hints stay narrow on purpose — "façade" would swallow real photos.
PLAN_HINTS = ("plan", "cadastr", "croquis", "implantation", "masse")


def is_plan(rel):
    parts = rel.replace("\\", "/").lower().split("/")
    if "plans" in parts[:-1]:
        return True
    return any(h in _deaccent(parts[-1]) for h in PLAN_HINTS)


def doc_dirs(listing_id):
    base = os.path.join(LISTINGS_DIR, listing_id)
    return [base, os.path.join(base, "docs")]


def list_docs(listing_id):
    """Sorted PDFs in the listing folder (+ /docs), each {rel, size} with rel
    relative to the listing folder."""
    if not SLUG_RE.match(listing_id or ""):
        return []
    base = os.path.join(LISTINGS_DIR, listing_id)
    out = []
    for d in doc_dirs(listing_id):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if os.path.splitext(name)[1].lower() not in DOC_EXTS:
                continue
            full = os.path.join(d, name)
            if os.path.isfile(full):
                out.append({"rel": os.path.relpath(full, base),
                            "size": os.path.getsize(full)})
    return sorted(out, key=lambda m: m["rel"])


def list_photos(listing_id):
    """Sorted media (image + video) in the listing folder (+ /photos), each a
    dict {rel, kind} with rel a path relative to that folder and kind image|video."""
    if not SLUG_RE.match(listing_id or ""):
        return []
    base = os.path.join(LISTINGS_DIR, listing_id)
    out = []
    for d in photo_dirs(listing_id):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            ext = os.path.splitext(name)[1].lower()
            if ext not in MEDIA_EXTS:
                continue
            full = os.path.join(d, name)
            if os.path.isfile(full):
                rel = os.path.relpath(full, base)
                out.append({"rel": rel,
                            "kind": "video" if ext in VIDEO_EXTS else "image",
                            "plan": is_plan(rel)})
    # plans last, so the combined lightbox order matches the two sections
    return sorted(out, key=lambda m: (m["plan"], m["rel"]))


def heic_to_jpeg(src):
    """Transcode a HEIC/HEIF file to a cached JPEG (macOS sips). Returns the
    cached path, or None if conversion is unavailable."""
    try:
        st = os.stat(src)
        key = hashlib.sha1(("%s:%s" % (src, st.st_mtime)).encode("utf-8")).hexdigest() + ".jpg"
        os.makedirs(PHOTO_CACHE, exist_ok=True)
        dst = os.path.join(PHOTO_CACHE, key)
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            return dst
        subprocess.run(["sips", "-s", "format", "jpeg", src, "--out", dst],
                       check=True, capture_output=True, timeout=60)
        return dst if os.path.exists(dst) and os.path.getsize(dst) > 0 else None
    except Exception:
        return None


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Brouillons — the shortlist-before-the-shortlist
# --------------------------------------------------------------------------
# Ads the user wants to look at later. Unlike a listing, a draft has no md file
# and no research behind it: it is typed straight into the grid and lives only
# in the db. Keep the field set small on purpose — the moment a draft deserves
# more, it should graduate into a real private/listings/<slug>.md dossier.

# The server is threaded, so two quick clicks on "Nouvelle annonce" ran
# load_db → new_draft → save_db concurrently and both handed out the same
# B-code (and the second save dropped the first row). Every draft mutation
# takes this lock for its whole read-modify-write.
DRAFT_LOCK = threading.Lock()

DRAFT_STATUSES = ["attente", "visite", "verifie", "ecarte", "promu"]
# Call-and-visit priority, three levels on purpose: a longer scale gets used as
# a ranking and stops meaning anything. "" = not triaged yet.
DRAFT_PRIORITIES = ["p1", "p2", "p3"]
DRAFT_TEXT_FIELDS = {"url", "city", "agency", "contact", "note"}
DRAFT_NUM_FIELDS = {"price", "surface", "land_surface"}
# visit_at holds what an <input type="datetime-local"> produces: "YYYY-MM-DDTHH:MM"
# (local wall-clock, no timezone — a viewing is an appointment, not an instant).
# The date alone is accepted too, for a slot whose hour is not fixed yet.
DRAFT_WHEN_FORMATS = ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")

# Ads are read in thousands ("749" for 749 000 €) and that is how the price gets
# typed. No house in this search costs under 10 000 €, so a value below that is
# unambiguously k€ — scale it up, and the grid immediately shows the full number
# back so the correction is visible rather than silent.
DRAFT_PRICE_K_CEILING = 10000


def clean_visit_at(val):
    """Normalise a viewing slot, or None if it is not a real date/time.

    Shape-checking with a regex let "2026-13-45T99:99" through, so parse it.
    """
    val = str(val).strip().replace(" ", "T")
    for fmt in DRAFT_WHEN_FORMATS:
        try:
            parsed = datetime.strptime(val, fmt)
        except ValueError:
            continue
        return parsed.strftime("%Y-%m-%d" if fmt == "%Y-%m-%d" else "%Y-%m-%dT%H:%M")
    return None


def dedupe_suggestions(values):
    """Suggestion list from `values`, first spelling of each name winning.

    Keyed on a loose form so "Saint Germain en Laye" typed into a listing does
    not sit next to the town sheet's "Saint-Germain-en-Laye" — which is why
    callers pass their most canonical source first.
    """
    seen = {}
    for name in values:
        name = (name or "").strip()
        key = re.sub(r"[^a-z0-9]+", "", _deaccent(name).lower())
        if name and key and key not in seen:
            seen[key] = name
    return sorted(seen.values(), key=lambda n: _deaccent(n).lower())


def draft_cities(db):
    """Commune names to suggest in the Brouillons grid: the target towns of the
    Villes ciblées tab first, then the communes of researched listings, then
    whatever was typed into other drafts. A suggestion, never a constraint —
    the field stays free text, because a lead can turn up anywhere."""
    towns = [re.sub(r"\s*\([^)]*\)\s*$", "", (t.get("title") or "").strip())
             for t in list_towns()]                                  # drop the "(78230)" suffix
    listings = [r.get("commune") for r in db.get("listings", {}).values()]
    typed = [d.get("city") for d in db.get("drafts", {}).values()]
    return dedupe_suggestions(towns + listings + typed)


def short_agency(name):
    """Trade name only, for the suggestion list.

    A listing's `agency` is written for the dossier and carries the legal tail
    ("Foncia Transaction Marly-le-Roi (FONCIA TRANSACTION FRANCE, RCS 503698664,
    …)"). Whole, it is unusable in a grid cell — and it hid that this agency is
    the same "iad France" already typed two rows up.
    """
    name = (name or "").strip()
    for sep in (" — ", " – ", " - ", " ("):
        cut = name.find(sep)
        if cut > 0:
            name = name[:cut]
    return name.strip(" ,;·").strip()[:60]


def draft_agencies(db):
    """Agency names to suggest: the ones on researched listings (read off the ad,
    so properly spelled) before the ones typed here."""
    listings = [short_agency(r.get("agency")) for r in db.get("listings", {}).values()]
    typed = [d.get("agency") for d in db.get("drafts", {}).values()]
    return dedupe_suggestions(listings + typed)


def draft_price_m2(rec):
    """€/m² of a draft, or None while price or surface is missing."""
    price, surface = rec.get("price"), rec.get("surface")
    if not price or not surface:
        return None
    return round(price / surface)


def with_derived(rec):
    out = dict(rec)
    out["price_per_m2"] = draft_price_m2(rec)
    out.setdefault("priority", "")   # rows written before priorities existed
    return out


def draft_code(n):
    return "B%02d" % n


def new_draft(db):
    """Blank row, with the next free B-code. Codes are never reused."""
    used = set()
    for d in db.get("drafts", {}).values():
        c = str(d.get("code") or "")
        if c.startswith("B") and c[1:].isdigit():
            used.add(int(c[1:]))
    n = 1
    while n in used:
        n += 1
    rec = {
        "id": uuid.uuid4().hex[:12],
        "code": draft_code(n),
        "url": "", "price": None, "city": "", "surface": None,
        "land_surface": None, "contact": "", "agency": "", "note": "",
        "visit_at": "", "status": "attente", "priority": "",
        "created": now_iso(), "updated": now_iso(),
    }
    db["drafts"][rec["id"]] = rec
    return rec


def apply_draft_patch(rec, body):
    """Copy the writable fields of `body` onto `rec`. Returns an error string,
    or None when the patch applied cleanly."""
    for key in DRAFT_TEXT_FIELDS:
        if key in body:
            val = body[key]
            cap = 4000 if key == "note" else 500   # note holds free prose, the rest are labels
            rec[key] = "" if val is None else str(val).strip()[:cap]
    for key in DRAFT_NUM_FIELDS:
        if key in body:
            val = body[key]
            if val in (None, ""):
                rec[key] = None
                continue
            try:
                num = float(str(val).replace(" ", "").replace("\u202f", "").replace(",", "."))
            except (TypeError, ValueError):
                return "invalid " + key
            if num < 0:
                return "invalid " + key
            if key == "price" and 0 < num < DRAFT_PRICE_K_CEILING:
                num *= 1000
            rec[key] = num
    if "visit_at" in body:
        raw = "" if body["visit_at"] is None else str(body["visit_at"]).strip()
        if not raw:
            rec["visit_at"] = ""
        else:
            when = clean_visit_at(raw)
            if when is None:
                return "invalid visit_at"
            rec["visit_at"] = when
    if "status" in body:
        if body["status"] not in DRAFT_STATUSES:
            return "invalid status"
        rec["status"] = body["status"]
    if "priority" in body:
        raw = "" if body["priority"] is None else str(body["priority"]).strip().lower()
        if raw and raw not in DRAFT_PRIORITIES:
            return "invalid priority"
        rec["priority"] = raw
    rec["updated"] = now_iso()
    return None


# --------------------------------------------------------------------------
# DB persistence
# --------------------------------------------------------------------------

def load_db():
    if not os.path.exists(DB_PATH):
        return {"version": 1, "listings": {}, "ignored": [], "drafts": {}}
    with open(DB_PATH, "r", encoding="utf-8") as f:
        db = json.load(f)
    db.setdefault("ignored", [])  # ids the user removed; import skips them (md stays on disk)
    db.setdefault("drafts", {})   # Brouillons tab: ads to look at later, typed straight into the grid
    for d in db["drafts"].values():
        d.setdefault("visit_at", "")
    settings = db.setdefault("settings", {})
    to = settings.get("town_order")  # user's drag-reordered town list (shared)
    if not isinstance(to, list):
        settings["town_order"] = []
    else:
        settings["town_order"] = [t for t in to if isinstance(t, str)]
    cols = settings.get("columns")
    if not isinstance(cols, list) or not cols:
        settings["columns"] = list(DEFAULT_COLUMNS)
    else:
        settings["columns"] = [c for c in cols if c in ALLOWED_COLUMNS] or list(DEFAULT_COLUMNS)
    # One-shot: surface the newly added "mitoyennete" column in existing saved
    # layouts (runs once; if the user then hides it, the flag keeps it hidden).
    if not db.get("_mig_mitoyennete_col"):
        vc = settings["columns"]
        if "mitoyennete" not in vc:
            at = vc.index("commune") + 1 if "commune" in vc else len(vc)
            vc.insert(at, "mitoyennete")
        db["_mig_mitoyennete_col"] = True
    # Same one-shot for the key-date columns (visits / offer date).
    if not db.get("_mig_dates_cols"):
        vc = settings["columns"]
        at = vc.index("status") + 1 if "status" in vc else len(vc)
        for key in ("visits", "offer_date"):
            if key not in vc:
                vc.insert(at, key)
                at += 1
        db["_mig_dates_cols"] = True
    # One-shot: surface the enrich.py columns on existing installs (the visible
    # set is persisted, so DEFAULT_COLUMNS alone only helps a brand-new db).
    if not db.get("_mig_enrich_cols"):
        vc = settings["columns"]
        if "flags" not in vc:
            vc.insert(vc.index("commune") + 1 if "commune" in vc else len(vc), "flags")
        if "spread" not in vc:
            at = vc.index("works_total") if "works_total" in vc else len(vc)
            vc.insert(at, "spread")
        db["_mig_enrich_cols"] = True
    # Migrations: "active" status folded into "researching"; new fields defaulted.
    for rec in db.get("listings", {}).values():
        if rec.get("status") == "active":
            rec["status"] = "researching"
        rec.setdefault("land_surface", None)
        rec.setdefault("price_min", None)
        rec.setdefault("price_offer", None)
        rec.setdefault("works", [])
        rec.setdefault("works_total", works_total(rec.get("works")))
        rec.setdefault("links", [])
        rec.setdefault("overrides", [])
        rec.setdefault("agency", None)
        rec.setdefault("contact", None)
        rec.setdefault("prev_status", None)
        rec.setdefault("value_m2_post", None)
        rec.setdefault("criteria_state", {})
        rec["visits"] = clean_dates(rec.get("visits"))
        rec.setdefault("offer_date", None)
        if "mitoyennete" not in rec:  # seed once from the md; user-editable thereafter
            rec["mitoyennete"] = extract_mitoyennete(rec.get("md_body", ""), rec.get("type"))
    return db


def save_db(db):
    os.makedirs(PRIVATE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=PRIVATE_DIR, prefix=".dashboard_db_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DB_PATH)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# --------------------------------------------------------------------------
# Markdown import heuristics
# --------------------------------------------------------------------------

def parse_section(body, header_prefix):
    """Text of the first '## ...' section whose header starts with header_prefix
    (case-insensitive), up to (not including) the next '## ' header."""
    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().lower().startswith(header_prefix.lower()):
            start = i + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end])


def extract_title(body):
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def extract_commune(text):
    m = re.search(r"\d{5}\s+([A-ZÀ-Ü][\wÀ-ÿ'\- ]+)", text)
    if not m:
        return None
    commune = re.split(r"[—,\n]", m.group(1))[0].strip()
    return commune or None


def parse_fr_number(s):
    if s is None:
        return None
    s = s.strip().replace(" ", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "")
    else:
        # French: comma + exactly 3 digits = thousands separator (44,003 → 44003);
        # comma + 1-2 digits = decimal (72,7 → 72.7).
        if re.fullmatch(r"\d{1,3}(,\d{3})+", s):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


PARCEL_LINE_RE = re.compile(r"parcel|parcelle|contenance", re.IGNORECASE)


def extract_surface(*texts):
    """Scan texts in priority order; skip parcel/contenance lines and
    implausible unit surfaces (outside 8-2000 m²)."""
    for text in texts:
        if not text:
            continue
        for line in text.splitlines():
            if PARCEL_LINE_RE.search(line):
                continue
            m = re.search(r"(\d{1,3}(?:,\d{3})+|\d+(?:[.,]\d+)?)\s*m²", line)
            if m:
                val = parse_fr_number(m.group(1))
                if val and 8 <= val <= 2000:
                    return val
    return None


def extract_land_surface(body):
    """m² figure on parcel/contenance/terrain lines = land, not living surface."""
    for line in body.splitlines():
        if not (PARCEL_LINE_RE.search(line) or re.search(r"terrain", line, re.IGNORECASE)):
            continue
        m = re.search(r"(\d{1,3}(?:,\d{3})+|\d+(?:[.,]\d+)?)\s*m²", line)
        if m:
            val = parse_fr_number(m.group(1))
            if val and 50 <= val <= 200000:
                return val
    return None


def extract_type(text):
    low = text.lower()
    if re.search(r"\bapt\b|appartement", low):
        return "apartment"
    if re.search(r"maison|pavillon|villa", low):  # a house with an "étage" is still a house
        return "house"
    if re.search(r"étage|etage", low):
        return "apartment"
    if "terrain" in low and not re.search(r"maison|appartement", low):
        return "land"
    return "house"


def extract_mitoyennete(body, ptype):
    """Heuristic seed for whether a house is semi-detached/terraced (mitoyenne)
    or free-standing (individuelle). Only meaningful for houses; user-editable."""
    if ptype != "house":
        return None
    low = (body or "").lower()
    if re.search(r"non\s+mitoyen|sans\s+mitoyen|aucune\s+mitoyen", low):
        return "individuelle"
    if re.search(r"mitoyen|jumel[ée]e?|accol[ée]e?|maison\s+de\s+ville", low):
        return "mitoyenne"
    if re.search(r"maison\s+individuelle|ind[ée]pendante|quatre\s+façades|pavillon\s+isol", low):
        return "individuelle"
    return None


def extract_dpe(*texts):
    for text in texts:
        if not text:
            continue
        # "**D / D**" and also the usual "**D / GES D**" spelling, with an
        # optional "Étiquette " / "DPE " lead-in inside the bold ("**Étiquette
        # E / GES D**" — how the H11 note spells it).
        m = re.search(r"\*\*\s*(?:[EÉ]tiquette|DPE)?\s*([A-G])\s*/\s*(?:GES\s*)?[A-G]\s*\*\*", text)
        if m:
            return m.group(1).upper()
    for text in texts:
        if not text:
            continue
        # \W (not \D) between "DPE" and the letter: \D would happily run through
        # the letters of a word and match its last character — "## DPE / Record
        # ADEME" was being read as DPE = E, the final E of "ADEME".
        m = re.search(r"DPE\b\W{0,20}?([A-G])\b", text)
        if m:
            return m.group(1).upper()
    return None


def extract_price(body):
    for line in body.splitlines():
        if re.search(r"prix|asking|price", line, re.IGNORECASE):
            m = re.search(r"€\s?([\d\s ,.]+?)\s*k\b", line, re.IGNORECASE)
            if m:
                val = parse_fr_number(m.group(1))
                if val:
                    return val * 1000
            m = re.search(r"€\s?([\d\s ,.]+)", line)
            if m:
                val = parse_fr_number(m.group(1))
                if val:
                    return val
            m = re.search(r"([\d\s ]+)\s?€", line)
            if m:
                val = parse_fr_number(m.group(1))
                if val:
                    return val
    return None


def extract_facts(identity_section):
    facts = {}
    for line in identity_section.splitlines():
        line = line.strip()
        m = re.match(r"^-\s+\*{0,2}([^:*]+?)\*{0,2}:\s*(.+)$", line)
        if m:
            key = m.group(1).strip()
            val = m.group(2).strip()[:200]
            if key:
                facts[key] = val
    return facts


def extract_agency_contact(facts):
    """Pull an agency name / contact person out of the Identity bullets when the
    announce carried them (keys like 'Agence', 'Agency', 'Contact', 'Négociateur',
    'Interlocuteur'). Returns (agency, contact) — each None if absent."""
    agency = contact = None
    for k, v in (facts or {}).items():
        kl = k.lower()
        val = re.sub(r"[*`]", "", str(v)).strip()[:200] or None
        if agency is None and ("agence" in kl or "agency" in kl):
            agency = val
        elif contact is None and any(w in kl for w in
                ("contact", "interlocuteur", "négociateur", "negociateur", "agent")):
            contact = val
    return agency, contact


def extract_status_verdict_seed(body):
    m = re.search(r"Status:\s*\*\*(.*?)\*\*", body)
    verdict_text = m.group(1).strip() if m else None
    status = "researching"
    if verdict_text:
        low = verdict_text.lower()
        if "archiv" in low:
            status = "archived"
        elif "reject" in low or "rejeté" in low:
            status = "rejected"
        elif "offre" in low or "offer" in low:
            status = "offer"
        elif "visit" in low or "visité" in low:  # "visité" / "visite prévue"
            status = "visit-planned" if ("prévu" in low or "prevu" in low or "planned" in low) else "visited"
        else:
            status = "researching"
    else:
        vsec = parse_section(body, "## verdict")
        first_line = next((l.strip() for l in vsec.splitlines() if l.strip()), None)
        verdict_text = first_line
    return status, verdict_text


def parse_listing_md(path):
    with open(path, "r", encoding="utf-8") as f:
        body = f.read()
    title = extract_title(body) or os.path.splitext(os.path.basename(path))[0]
    h1 = title
    identity_section = parse_section(body, "## identity")
    dpe_section = parse_section(body, "## dpe")
    commune = extract_commune(h1) or extract_commune(body)
    ptype = extract_type(h1 + "\n" + body)
    surface = extract_surface(h1, dpe_section, identity_section)
    land_surface = extract_land_surface(body)
    dpe = extract_dpe(identity_section, dpe_section, body)
    mitoyennete_seed = extract_mitoyennete(body, ptype)
    price = extract_price(body)
    if price is not None and price < 20000:
        price = None  # too small for a property asking price — likely a €/m² or works figure
    facts = extract_facts(identity_section)
    agency, contact = extract_agency_contact(facts)
    status_seed, verdict_seed = extract_status_verdict_seed(body)
    price_per_m2 = round(price / surface, 0) if price and surface else None
    return {
        "title": title,
        "commune": commune,
        "type": ptype,
        "surface": surface,
        "land_surface": land_surface,
        "dpe": dpe,
        "price": price,
        "price_per_m2": price_per_m2,
        "agency": agency,
        "contact": contact,
        "facts": facts,
        "md_body": body,
        "_status_seed": status_seed,
        "_verdict_seed": verdict_seed,
        "_mitoyennete_seed": mitoyennete_seed,
    }


def import_listings(db):
    added = 0
    updated = 0
    listings = db.setdefault("listings", {})
    ignored = set(db.get("ignored") or [])
    if not os.path.isdir(LISTINGS_DIR):
        return db, added, updated
    for path in sorted(glob.glob(os.path.join(LISTINGS_DIR, "*.md"))):
        listing_id = os.path.splitext(os.path.basename(path))[0]
        if listing_id in ignored:
            continue  # user removed this one from the dashboard
        mtime = os.path.getmtime(path)
        parsed = parse_listing_md(path)
        existing = listings.get(listing_id)
        ts = now_iso()
        rel_path = os.path.relpath(path, REPO_ROOT)
        if existing is None:
            listings[listing_id] = {
                "id": listing_id,
                "md_path": rel_path,
                "title": parsed["title"],
                "commune": parsed["commune"],
                "type": parsed["type"],
                "price": parsed["price"],
                "surface": parsed["surface"],
                "land_surface": parsed["land_surface"],
                "price_per_m2": parsed["price_per_m2"],
                "price_min": None,
                "price_offer": None,
                "visits": [],
                "offer_date": None,
                "works": [],
                "works_total": None,
                "links": [],
                "dpe": parsed["dpe"],
                "mitoyennete": parsed["_mitoyennete_seed"],
                "agency": parsed["agency"],
                "contact": parsed["contact"],
                "rating": 0,
                "status": parsed["_status_seed"],
                "verdict": parsed["_verdict_seed"],
                "tags": [],
                "comment": "",
                "facts": parsed["facts"],
                "md_body": parsed["md_body"],
                "md_mtime": mtime,
                "overrides": [],
                "created": ts,
                "updated": ts,
            }
            added += 1
        else:
            changed = existing.get("md_mtime") != mtime
            overrides = set(existing.get("overrides") or [])
            existing["md_path"] = rel_path
            existing["title"] = parsed["title"]
            existing["commune"] = parsed["commune"]
            existing["type"] = parsed["type"]
            existing["dpe"] = parsed["dpe"]
            for field in OVERRIDABLE_FIELDS | OVERRIDABLE_STR_FIELDS:
                if field not in overrides:
                    existing[field] = parsed[field]
            p, s = existing.get("price"), existing.get("surface")
            existing["price_per_m2"] = round(p / s, 0) if p and s else None
            existing["facts"] = parsed["facts"]
            existing["md_body"] = parsed["md_body"]
            existing["md_mtime"] = mtime
            if changed:
                existing["updated"] = ts
                updated += 1
    ensure_codes(db)
    return db, added, updated


# --------------------------------------------------------------------------
# Target-towns study (reference data — parsed fresh from private/towns/*.md,
# never persisted in the db; edit the md and it shows on next tab open).
# --------------------------------------------------------------------------

# Default comparison order (used until the user drags to reorder — that choice is
# saved server-side in settings["town_order"] and shared across everyone). Unknown
# towns fall after, alpha.
TOWN_ORDER = [
    "jouy-en-josas", "bievres", "igny", "chaville",
    "noisy-le-roi", "bailly", "l-etang-la-ville", "marly-le-roi",
    "mareil-marly", "louveciennes", "le-pecq", "chatou",
    "bougival", "croissy-sur-seine", "saint-germain-en-laye", "marnes-la-coquette",
    "viroflay", "porchefontaine",
    "vaucresson", "ville-d-avray", "la-celle-saint-cloud", "sartrouville",
    "buc", "le-port-marly", "chavenay", "bois-d-arcy",
]


# --------------------------------------------------------------------------
# Criteria checklist (tracked criteria.md — generic knowledge, no PII, so it
# lives outside private/ like towns/). Two H2 parts ("Immuable" / "Modifiable"),
# H3 groups, and "- **label** — detail `cost`" items.
# --------------------------------------------------------------------------
CRITERIA_PATH = os.path.join(REPO_ROOT, "criteria.md")
_CRIT_ITEM_RE = re.compile(r"^-\s+\*\*(?P<label>.+?)\*\*\s*(?P<rest>.*)$")


def _crit_id(part, group, label):
    """Stable id so ticks survive re-wording of the surrounding prose."""
    raw = f"{part}/{group}/{label}".lower()
    raw = "".join(c for c in unicodedata.normalize("NFD", raw)
                  if unicodedata.category(c) != "Mn")
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return hashlib.sha1(slug.encode("utf-8")).hexdigest()[:10]


def parse_criteria():
    if not os.path.exists(CRITERIA_PATH):
        return {"intro": "", "parts": []}
    with open(CRITERIA_PATH, "r", encoding="utf-8") as f:
        body = f.read()
    lines = body.splitlines()
    intro, parts = [], []
    part = group = None
    for ln in lines:
        if ln.startswith("# ") and not ln.startswith("## "):
            continue
        if ln.startswith("## "):
            part = {"title": ln[3:].strip(), "groups": []}
            parts.append(part)
            group = None
            continue
        if ln.startswith("### ") and part is not None:
            group = {"title": ln[4:].strip(), "items": []}
            part["groups"].append(group)
            continue
        m = _CRIT_ITEM_RE.match(ln.strip())
        if m and part is not None:
            if group is None:  # tolerate items before any H3
                group = {"title": "", "items": []}
                part["groups"].append(group)
            label = m.group("label").strip()
            rest = m.group("rest").strip()
            rest = re.sub(r"^[—–-]\s*", "", rest)
            cost = None
            cm = re.search(r"`([^`]+)`\s*$", rest)
            if cm:
                cost = cm.group(1).strip()
                rest = rest[: cm.start()].strip()
            flags = []
            for tag, key in (("[éliminatoire", "eliminatoire"), ("[visite]", "visite")):
                if tag in rest or tag in label:
                    flags.append(key)
            # the markers become badges in the UI — strip them from the prose so
            # they don't read twice ("[visite] — lire la table des anomalies…")
            strip = re.compile(r"\*{0,2}\[(?:éliminatoire|visite)[^\]]*\]\*{0,2}")
            label = strip.sub("", label).strip(" —-")
            rest = strip.sub("", rest).strip()
            rest = re.sub(r"^[—–-]\s*", "", rest).strip()
            group["items"].append({
                "id": _crit_id(part["title"], group["title"], label),
                "label": label,
                "detail": rest.rstrip(" —-"),
                "cost": cost,
                "flags": flags,
            })
            continue
        if part is None and ln.strip():
            intro.append(ln)
    for p in parts:
        p["count"] = sum(len(g["items"]) for g in p["groups"])
    return {"intro": "\n".join(intro).strip(), "parts": parts}


def parse_town_md(path):
    with open(path, "r", encoding="utf-8") as f:
        body = f.read()
    town_id = os.path.splitext(os.path.basename(path))[0]
    title = extract_title(body) or town_id
    reperes = parse_section(body, "## repères") or parse_section(body, "## reperes")
    facts = extract_facts(reperes)
    return {
        "id": town_id,
        "title": title,
        # ordered list keeps the md's bullet order for the comparison rows
        "reperes": [{"k": k, "v": v} for k, v in facts.items()],
        "md_body": body,
    }


def list_towns(saved_order=None):
    """Parsed town cards, ordered by the user's saved drag order first, then the
    built-in TOWN_ORDER default, then alphabetically for anything unlisted."""
    if not os.path.isdir(TOWNS_DIR):
        return []
    towns = [parse_town_md(p) for p in glob.glob(os.path.join(TOWNS_DIR, "*.md"))]
    saved = list(saved_order or [])
    saved_rank = {tid: i for i, tid in enumerate(saved)}

    def sort_key(t):
        if t["id"] in saved_rank:
            return (0, saved_rank[t["id"]], "")
        try:
            rank = TOWN_ORDER.index(t["id"])
        except ValueError:
            rank = len(TOWN_ORDER)
        return (1, rank, t["title"].lower())

    return sorted(towns, key=sort_key)


CODE_RE = re.compile(r"^H(\d+)$")


def ensure_codes(db):
    """Give every listing a short, sortable reference code (H01, H02, …).
    Assigned once, oldest first; never reused even after a delete."""
    listings = db.get("listings", {})
    maxn = 0
    for rec in listings.values():
        m = CODE_RE.match(rec.get("code") or "")
        if m:
            maxn = max(maxn, int(m.group(1)))
    missing = [r for r in listings.values() if not CODE_RE.match(r.get("code") or "")]
    missing.sort(key=lambda r: (r.get("created", ""), r.get("id", "")))
    for r in missing:
        maxn += 1
        r["code"] = "H%02d" % maxn
    return db


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "HouseHuntDashboard/0.1"
    protocol_version = "HTTP/1.1"  # keep-alive + proper Range streaming for <video>

    def log_message(self, fmt, *args):
        pass  # quiet; this is a local-only tool

    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        # local dev tool: never let the browser serve a stale page (a cached copy
        # would run old JS — e.g. missing the town drag-reorder/save wiring)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(data)

    def _send_media(self, path, content_type, extra=None):
        """Serve a file with HTTP Range support (needed for <video> seeking and
        for the browser's PDF viewer). `extra` adds response headers."""
        try:
            size = os.path.getsize(path)
        except OSError:
            self.send_error(404)
            return
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                elif m.group(2):  # suffix range: last N bytes
                    start = max(0, size - int(m.group(2)))
                start = min(start, size - 1)
                end = min(end, size - 1)
                partial = True
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # client seeked away / closed — normal for video
                remaining -= len(chunk)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_file(HTML_PATH, "text/html; charset=utf-8")
            return
        if path == "/api/listings":
            db = load_db()
            items = sorted(db["listings"].values(), key=lambda r: r.get("updated", ""), reverse=True)
            self._send_json({"listings": [decorate(r) for r in items]})
            return
        if path == "/api/drafts":
            db = load_db()
            items = sorted(db["drafts"].values(), key=lambda d: d.get("code", ""))
            self._send_json({"drafts": [with_derived(d) for d in items],
                             "cities": draft_cities(db),
                             "agencies": draft_agencies(db)})
            return
        if path == "/api/criteria":
            self._send_json(parse_criteria())
            return
        if path == "/api/price-history":
            # written by scripts/enrich_towns.py; tracked, public-record data
            hp = os.path.join(TOWNS_DIR, "price_history.json")
            if not os.path.exists(hp):
                self._send_json({"years": [], "communes": {}})
                return
            with open(hp, "r", encoding="utf-8") as f:
                self._send_json(json.load(f))
            return
        if path == "/api/towns":
            db = load_db()
            self._send_json({"towns": list_towns(db["settings"].get("town_order"))})
            return
        if path == "/api/settings":
            db = load_db()
            self._send_json({
                "columns": db["settings"]["columns"],
                "all_columns": ALLOWED_COLUMNS,
                "town_order": db["settings"].get("town_order", []),
            })
            return
        m = re.match(r"^/api/listings/([^/]+)/photos$", path)
        if m:
            listing_id = m.group(1)
            media = list_photos(listing_id)
            photos = [{"name": item["rel"], "kind": item["kind"], "plan": item["plan"],
                       "url": "/photos/" + listing_id + "/" + quote(item["rel"])}
                      for item in media]
            self._send_json({"photos": photos})
            return
        m = re.match(r"^/photos/([^/]+)/(.+)$", path)
        if m:
            self._serve_photo(m.group(1), m.group(2))
            return
        m = re.match(r"^/api/listings/([^/]+)/docs$", path)
        if m:
            listing_id = m.group(1)
            docs = [{"name": d["rel"], "size": d["size"],
                     "title": doc_title(d["rel"]),
                     "url": "/docs/" + listing_id + "/" + quote(d["rel"])}
                    for d in list_docs(listing_id)]
            self._send_json({"docs": docs})
            return
        m = re.match(r"^/docs/([^/]+)/(.+)$", path)
        if m:
            self._serve_doc(m.group(1), m.group(2))
            return
        m = re.match(r"^/api/listings/([^/]+)$", path)
        if m:
            db = load_db()
            rec = db["listings"].get(m.group(1))
            if rec is None:
                self._send_json({"error": "not found"}, 404)
            else:
                self._send_json(rec)
            return
        self.send_error(404)

    def _serve_photo(self, listing_id, rel):
        # Guard against path traversal: slug-checked id, and the resolved file
        # must stay inside the listing folder and be a known media type.
        rel = unquote(rel)
        if not SLUG_RE.match(listing_id or ""):
            self.send_error(404)
            return
        base = os.path.realpath(os.path.join(LISTINGS_DIR, listing_id))
        target = os.path.realpath(os.path.join(base, rel))
        if os.path.commonpath([base, target]) != base:
            self.send_error(404)
            return
        ext = os.path.splitext(target)[1].lower()
        if ext not in MEDIA_EXTS or not os.path.isfile(target):
            self.send_error(404)
            return
        if ext in HEIC_EXTS:
            jpg = heic_to_jpeg(target)
            if jpg:
                self._send_media(jpg, "image/jpeg")  # transcoded for browser display
                return
            # fall through: serve raw HEIC (Safari can render it)
        self._send_media(target, MEDIA_EXTS[ext])

    def _serve_doc(self, listing_id, rel):
        # Same traversal guard as the photos: slug-checked id, resolved file
        # must stay inside the listing folder, and be a known document type.
        rel = unquote(rel)
        if not SLUG_RE.match(listing_id or ""):
            self.send_error(404)
            return
        base = os.path.realpath(os.path.join(LISTINGS_DIR, listing_id))
        target = os.path.realpath(os.path.join(base, rel))
        if os.path.commonpath([base, target]) != base:
            self.send_error(404)
            return
        ext = os.path.splitext(target)[1].lower()
        if ext not in DOC_EXTS or not os.path.isfile(target):
            self.send_error(404)
            return
        # inline → the browser's built-in PDF viewer renders it in the iframe
        self._send_media(target, DOC_EXTS[ext],
                         extra={"Content-Disposition": "inline"})

    def do_HEAD(self):
        path = urlparse(self.path).path
        m = re.match(r"^/photos/([^/]+)/(.+)$", path)
        if m:
            self._serve_photo(m.group(1), m.group(2))
            return
        m = re.match(r"^/docs/([^/]+)/(.+)$", path)
        if m:
            self._serve_doc(m.group(1), m.group(2))
            return
        self.send_error(404)

    def _read_json(self):
        """Request body as a dict, or None after having already sent the error."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": "invalid json"}, 400)
            return None
        if not isinstance(body, dict):
            self._send_json({"error": "invalid body"}, 400)
            return None
        return body

    def do_PATCH(self):
        m = re.match(r"^/api/drafts/([^/]+)$", urlparse(self.path).path)
        if m:
            body = self._read_json()
            if body is None:
                return
            with DRAFT_LOCK:
                db = load_db()
                rec = db["drafts"].get(m.group(1))
                if rec is None:
                    self._send_json({"error": "not found"}, 404)
                    return
                err = apply_draft_patch(rec, body)
                if err:
                    self._send_json({"error": err}, 400)
                    return
                save_db(db)
            self._send_json(with_derived(rec))
            return
        m = re.match(r"^/api/listings/([^/]+)$", urlparse(self.path).path)
        if not m:
            self.send_error(404)
            return
        listing_id = m.group(1)
        db = load_db()
        rec = db["listings"].get(listing_id)
        if rec is None:
            self._send_json({"error": "not found"}, 404)
            return
        body = self._read_json()
        if body is None:
            return
        # {"archived": true/false} is a convenience toggle: archiving keeps the
        # current status in prev_status, un-archiving restores it (falling back
        # to "researching" for records archived before prev_status existed).
        if "archived" in body:
            if not isinstance(body["archived"], bool):
                self._send_json({"error": "archived must be a boolean"}, 400)
                return
            if body.pop("archived"):
                body["status"] = "archived"
            else:
                body["status"] = rec.get("prev_status") or "researching"
        if "status" in body and body["status"] not in STATUS_VALUES:
            self._send_json({"error": "invalid status"}, 400)
            return
        if "rating" in body:
            try:
                r = float(body["rating"])
            except (TypeError, ValueError):
                self._send_json({"error": "invalid rating"}, 400)
                return
            if r < 0 or r > 5:
                self._send_json({"error": "invalid rating"}, 400)
                return
        if "tags" in body and not isinstance(body["tags"], list):
            self._send_json({"error": "invalid tags"}, 400)
            return
        if "works" in body and not isinstance(body["works"], list):
            self._send_json({"error": "works must be a list"}, 400)
            return
        if "links" in body and not isinstance(body["links"], list):
            self._send_json({"error": "links must be a list"}, 400)
            return
        if "visits" in body and not isinstance(body["visits"], list):
            self._send_json({"error": "visits must be a list"}, 400)
            return
        for key in USER_DATE_FIELDS:
            if key in body and body[key] is not None:
                val = str(body[key]).strip()
                if val and not clean_dates([val]):
                    self._send_json({"error": "invalid " + key}, 400)
                    return
        for key in OVERRIDABLE_FIELDS | USER_NUMERIC_FIELDS:
            if key not in body:
                continue
            val = body[key]
            if val is not None:
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    self._send_json({"error": "invalid " + key}, 400)
                    return
                if val < 0:
                    self._send_json({"error": "invalid " + key}, 400)
                    return
        # Remember where a listing came from when it enters the archive, and drop
        # the memo once it leaves — so prev_status only ever describes an
        # archived record. Must run before rec["status"] is overwritten below.
        if "status" in body:
            was, now = rec.get("status"), body["status"]
            if now == "archived" and was != "archived":
                rec["prev_status"] = was
            elif now != "archived":
                rec["prev_status"] = None
        for key in USER_OWNED_FIELDS:
            if key in body:
                rec[key] = body[key]
        # pure user numeric fields (negotiation prices): stored as-is, nullable
        for key in USER_NUMERIC_FIELDS:
            if key in body:
                rec[key] = None if body[key] is None else float(body[key])
        # key dates: visits are normalised (valid ISO only, de-duplicated, sorted
        # so index 0 is the 1st visit); the offer date is a single nullable day
        if "visits" in body:
            rec["visits"] = clean_dates(body["visits"])
        for key in USER_DATE_FIELDS:
            if key in body:
                val = body[key]
                val = str(val).strip() if val is not None else ""
                rec[key] = val or None
        # renovation line-items: sanitize [{label, cost}], recompute the total
        if "works" in body:
            clean = []
            for item in body["works"]:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label", "")).strip()[:200]
                cost = item.get("cost")
                try:
                    cost = float(cost) if cost not in (None, "") else None
                except (TypeError, ValueError):
                    cost = None
                if cost is not None and cost < 0:
                    cost = None
                if label or cost is not None:
                    clean.append({"label": label, "cost": cost})
            rec["works"] = clean
            rec["works_total"] = works_total(clean)
        # announce links: keep only http(s) URLs, deduped, capped
        if "links" in body:
            seen, urls = set(), []
            for u in body["links"]:
                u = str(u).strip()[:500]
                if u.startswith(("http://", "https://")) and u not in seen:
                    seen.add(u)
                    urls.append(u)
            rec["links"] = urls[:20]
        overrides = set(rec.get("overrides") or [])
        for key in OVERRIDABLE_FIELDS:
            if key in body:
                if body[key] is None:
                    # clearing the value releases the override; next re-import re-derives it
                    rec[key] = None
                    overrides.discard(key)
                else:
                    rec[key] = float(body[key])
                    overrides.add(key)
        # agency / contact: same override contract, but strings. Blank clears it.
        for key in OVERRIDABLE_STR_FIELDS:
            if key in body:
                val = body[key]
                if val is None or str(val).strip() == "":
                    rec[key] = None
                    overrides.discard(key)
                else:
                    rec[key] = str(val).strip()[:200]
                    overrides.add(key)
        rec["overrides"] = sorted(overrides)
        p, s = rec.get("price"), rec.get("surface")
        rec["price_per_m2"] = round(p / s, 0) if p and s else None
        rec["updated"] = now_iso()
        db["listings"][listing_id] = rec
        save_db(db)
        self._send_json(rec)

    def do_DELETE(self):
        m = re.match(r"^/api/drafts/([^/]+)$", urlparse(self.path).path)
        if m:
            with DRAFT_LOCK:
                db = load_db()
                gone = db["drafts"].pop(m.group(1), None)
                if gone is None:
                    self.send_error(404)
                    return
                save_db(db)
                left = len(db["drafts"])
            self._send_json({"deleted": m.group(1), "total": left})
            return
        m = re.match(r"^/api/listings/([^/]+)$", urlparse(self.path).path)
        if not m:
            self.send_error(404)
            return
        listing_id = m.group(1)
        db = load_db()
        db["listings"].pop(listing_id, None)
        ignored = set(db.get("ignored") or [])
        ignored.add(listing_id)  # keep it out on future re-imports; the md file is left untouched
        db["ignored"] = sorted(ignored)
        save_db(db)
        self._send_json({"deleted": listing_id, "total": len(db["listings"])})

    def do_PUT(self):
        if urlparse(self.path).path != "/api/settings":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": "invalid json"}, 400)
            return
        db = load_db()
        # town_order: shared drag order for the Villes ciblées tab (list of town ids)
        if "town_order" in body:
            to = body.get("town_order")
            if not isinstance(to, list):
                self._send_json({"error": "town_order must be a list"}, 400)
                return
            seen_t, clean_t = set(), []
            for t in to:
                if isinstance(t, str) and t and t not in seen_t:
                    seen_t.add(t)
                    clean_t.append(t[:100])
            db["settings"]["town_order"] = clean_t[:200]
            # allow a town-order-only PUT (columns optional)
            if "columns" not in body:
                save_db(db)
                self._send_json({
                    "columns": db["settings"]["columns"],
                    "all_columns": ALLOWED_COLUMNS,
                    "town_order": db["settings"]["town_order"],
                })
                return
        cols = body.get("columns")
        if not isinstance(cols, list):
            self._send_json({"error": "columns must be a list"}, 400)
            return
        # keep known keys, drop dupes, preserve caller order; title always stays
        seen, clean = set(), []
        for c in cols:
            if c in ALLOWED_COLUMNS and c not in seen:
                seen.add(c)
                clean.append(c)
        if "title" not in seen:
            clean.insert(0, "title")
        if not clean:
            clean = list(DEFAULT_COLUMNS)
        db["settings"]["columns"] = clean
        save_db(db)
        self._send_json({
            "columns": clean,
            "all_columns": ALLOWED_COLUMNS,
            "town_order": db["settings"].get("town_order", []),
        })

    def do_POST(self):
        if urlparse(self.path).path == "/api/drafts":
            body = self._read_json()
            if body is None:
                return
            with DRAFT_LOCK:
                db = load_db()
                rec = new_draft(db)
                err = apply_draft_patch(rec, body) if body else None
                if err:
                    self._send_json({"error": err}, 400)
                    return
                save_db(db)
            self._send_json(with_derived(rec))
            return
        if urlparse(self.path).path == "/api/reimport":
            db = load_db()
            db, added, updated = import_listings(db)
            save_db(db)
            self._send_json({"added": added, "updated": updated, "total": len(db["listings"])})
            return
        self.send_error(404)


def lan_ips():
    """Best-effort list of this machine's LAN IPv4 addresses (no traffic sent)."""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 1))  # TEST-NET-1, unroutable; just reads the chosen local IP
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def main():
    parser = argparse.ArgumentParser(description="House Hunt local dashboard")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Bind address. Default 127.0.0.1 (this machine only). "
             "Use 0.0.0.0 to also reach it from other devices on your LAN (phone, tablet).",
    )
    parser.add_argument(
        "--lan", action="store_true",
        help="Shortcut for --host 0.0.0.0 (expose on the local network).",
    )
    args = parser.parse_args()
    host = "0.0.0.0" if args.lan else args.host

    db = load_db()
    db, added, updated = import_listings(db)
    save_db(db)
    print(f"Imported listings: {added} added, {updated} updated, {len(db['listings'])} total")

    server = ThreadingHTTPServer((host, args.port), Handler)
    if host in ("127.0.0.1", "localhost"):
        print(f"House Hunt dashboard (this machine only): http://127.0.0.1:{args.port}/")
    else:
        print(f"House Hunt dashboard bound on {host}:{args.port}")
        print(f"  · on this machine:  http://localhost:{args.port}/")
        for ip in lan_ips():
            print(f"  · on your network:  http://{ip}:{args.port}/")
        print("  ⚠ Exposed to your local network — anyone on the same Wi-Fi/LAN can open it")
        print("    (it shows your private/ research and has no password). Use only on a trusted network.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
