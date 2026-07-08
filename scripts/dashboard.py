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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
PRIVATE_DIR = os.path.join(REPO_ROOT, "private")
LISTINGS_DIR = os.path.join(PRIVATE_DIR, "listings")
DB_PATH = os.path.join(PRIVATE_DIR, "dashboard_db.json")
HTML_PATH = os.path.join(SCRIPT_DIR, "dashboard.html")

STATUS_VALUES = {"researching", "visit-planned", "visited", "offer", "rejected"}
USER_OWNED_FIELDS = {"rating", "status", "verdict", "tags", "comment"}
# Numeric fields derived from the md but that the user may correct by hand; once
# edited they are recorded in the record's "overrides" list and re-imports no
# longer touch them.
OVERRIDABLE_FIELDS = {"price", "surface", "land_surface"}
# Pure user-set numeric negotiation fields (never derived from the md): the
# minimum price the seller might accept and the offer the buyer is willing to make.
USER_NUMERIC_FIELDS = {"price_min", "price_offer"}

# Column-picker: every column the grid can show (the frontend holds the labels &
# renderers; the server only validates keys and persists the chosen visible set,
# so the choice is shared across everyone hitting this server — "all users").
ALLOWED_COLUMNS = [
    "code", "title", "commune", "type", "price", "price_min", "price_offer", "price_per_m2",
    "works_total", "land_surface", "surface", "dpe", "rating", "status", "verdict", "tags",
    "created", "updated",
]
DEFAULT_COLUMNS = [
    "code", "title", "commune", "price", "price_offer", "works_total", "land_surface", "surface",
    "dpe", "rating", "status", "updated",
]


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
PHOTO_CACHE = os.path.join(PRIVATE_DIR, ".media_cache")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def photo_dirs(listing_id):
    base = os.path.join(LISTINGS_DIR, listing_id)
    return [base, os.path.join(base, "photos")]


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
                out.append({"rel": os.path.relpath(full, base),
                            "kind": "video" if ext in VIDEO_EXTS else "image"})
    return sorted(out, key=lambda m: m["rel"])


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
# DB persistence
# --------------------------------------------------------------------------

def load_db():
    if not os.path.exists(DB_PATH):
        return {"version": 1, "listings": {}, "ignored": []}
    with open(DB_PATH, "r", encoding="utf-8") as f:
        db = json.load(f)
    db.setdefault("ignored", [])  # ids the user removed; import skips them (md stays on disk)
    settings = db.setdefault("settings", {})
    cols = settings.get("columns")
    if not isinstance(cols, list) or not cols:
        settings["columns"] = list(DEFAULT_COLUMNS)
    else:
        settings["columns"] = [c for c in cols if c in ALLOWED_COLUMNS] or list(DEFAULT_COLUMNS)
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
    if re.search(r"\bapt\b|appartement|étage|etage", low):
        return "apartment"
    if "terrain" in low and not re.search(r"maison|appartement", low):
        return "land"
    return "house"


def extract_dpe(*texts):
    for text in texts:
        if not text:
            continue
        m = re.search(r"\*\*\s*([A-G])\s*/\s*[A-G]\s*\*\*", text)
        if m:
            return m.group(1).upper()
    for text in texts:
        if not text:
            continue
        m = re.search(r"DPE\D{0,20}?([A-G])\b", text)
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


def extract_status_verdict_seed(body):
    m = re.search(r"Status:\s*\*\*(.*?)\*\*", body)
    verdict_text = m.group(1).strip() if m else None
    status = "researching"
    if verdict_text:
        low = verdict_text.lower()
        if "reject" in low or "rejeté" in low:
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
    price = extract_price(body)
    if price is not None and price < 20000:
        price = None  # too small for a property asking price — likely a €/m² or works figure
    facts = extract_facts(identity_section)
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
        "facts": facts,
        "md_body": body,
        "_status_seed": status_seed,
        "_verdict_seed": verdict_seed,
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
                "works": [],
                "works_total": None,
                "links": [],
                "dpe": parsed["dpe"],
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
            for field in OVERRIDABLE_FIELDS:
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
        self.end_headers()
        self.wfile.write(data)

    def _send_media(self, path, content_type):
        """Serve a file with HTTP Range support (needed for <video> seeking)."""
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
            self._send_json({"listings": items})
            return
        if path == "/api/settings":
            db = load_db()
            self._send_json({
                "columns": db["settings"]["columns"],
                "all_columns": ALLOWED_COLUMNS,
            })
            return
        m = re.match(r"^/api/listings/([^/]+)/photos$", path)
        if m:
            listing_id = m.group(1)
            media = list_photos(listing_id)
            photos = [{"name": item["rel"], "kind": item["kind"],
                       "url": "/photos/" + listing_id + "/" + item["rel"]} for item in media]
            self._send_json({"photos": photos})
            return
        m = re.match(r"^/photos/([^/]+)/(.+)$", path)
        if m:
            self._serve_photo(m.group(1), m.group(2))
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

    def do_HEAD(self):
        m = re.match(r"^/photos/([^/]+)/(.+)$", urlparse(self.path).path)
        if m:
            self._serve_photo(m.group(1), m.group(2))
            return
        self.send_error(404)

    def do_PATCH(self):
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
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": "invalid json"}, 400)
            return
        if not isinstance(body, dict):
            self._send_json({"error": "invalid body"}, 400)
            return
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
        for key in USER_OWNED_FIELDS:
            if key in body:
                rec[key] = body[key]
        # pure user numeric fields (negotiation prices): stored as-is, nullable
        for key in USER_NUMERIC_FIELDS:
            if key in body:
                rec[key] = None if body[key] is None else float(body[key])
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
        rec["overrides"] = sorted(overrides)
        p, s = rec.get("price"), rec.get("surface")
        rec["price_per_m2"] = round(p / s, 0) if p and s else None
        rec["updated"] = now_iso()
        db["listings"][listing_id] = rec
        save_db(db)
        self._send_json(rec)

    def do_DELETE(self):
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
        db = load_db()
        db["settings"]["columns"] = clean
        save_db(db)
        self._send_json({"columns": clean, "all_columns": ALLOWED_COLUMNS})

    def do_POST(self):
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
