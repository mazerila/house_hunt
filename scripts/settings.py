"""Central configuration: endpoint URLs, dataset ids, field maps, TTLs.

Every fact that the data providers might change (a dataset slug, a snake_case
field name, a rate limit) lives here so a future rename is a one-line edit.
"""
from __future__ import annotations

import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from the repo-root .env into os.environ.

    Lets `ANTHROPIC_API_KEY` (and optional overrides) be picked up by the CLI
    scripts without exporting them. Existing env vars win, so a real shell
    export always overrides the file.
    """
    path = os.path.join(_REPO_ROOT, ".env")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_dotenv()

# Privacy: no PII in outbound requests by default. A contact email is sent to
# open APIs (RNB `from` param, User-Agent) ONLY if explicitly opted in via
# HOUSE_HUNT_FROM_EMAIL in the gitignored .env — never hardcode one here.
CONTACT_EMAIL = os.environ.get("HOUSE_HUNT_FROM_EMAIL", "")
USER_AGENT = f"house-hunt/0.1 ({CONTACT_EMAIL})" if CONTACT_EMAIL else "house-hunt/0.1"

# --- Endpoints -------------------------------------------------------------
BAN_SEARCH = "https://api-adresse.data.gouv.fr/search/"
RNB_BASE = "https://rnb-api.beta.gouv.fr/api/alpha/buildings"
CADASTRE_PARCELLE = "https://apicarto.ign.fr/api/cadastre/parcelle"
GEORISQUES_BASE = "https://www.georisques.gouv.fr/api/v1"
DVF_BASE = "https://app.dvf.etalab.gouv.fr/api"

# ADEME DPE "logements existants (depuis juillet 2021)" — datafair API.
# NOTE: the live dataset id is meg-83tjwtg8dyz4vv7h1dqe (the older
# dpe-v2-logements-existants slug 404s). Verified during planning.
ADEME_DATASET = os.environ.get("HOUSE_HUNT_ADEME_DATASET", "meg-83tjwtg8dyz4vv7h1dqe")
ADEME_LINES = f"https://data.ademe.fr/data-fair/api/v1/datasets/{ADEME_DATASET}/lines"

# ADEME field names (snake_case). Centralised so a schema rename is one edit.
# Maps our normalized key -> the ADEME snake_case source field (verified live).
ADEME_FIELDS = {
    "numero_dpe": "numero_dpe",
    "id_rnb": "id_rnb",
    "etiquette_dpe": "etiquette_dpe",
    "etiquette_ges": "etiquette_ges",
    "conso_ep_m2": "conso_5_usages_par_m2_ep",
    "emission_ges_m2": "emission_ges_5_usages_par_m2",
    "surface_habitable": "surface_habitable_logement",
    "annee_construction": "annee_construction",
    "date_etablissement": "date_etablissement_dpe",
    "type_energie": "type_energie_principale_chauffage",
    "adresse_ban": "adresse_ban",
    "geopoint": "_geopoint",
}

# --- Anthropic (listing extraction) ---------------------------------------
ANTHROPIC_MODEL = os.environ.get("HOUSE_HUNT_LISTING_MODEL", "claude-opus-4-8")

# --- Browser for fetching listing pages ------------------------------------
# "firefox" (default), "chrome" (real Google Chrome via channel), "chromium".
BROWSER_ENGINE = os.environ.get("HOUSE_HUNT_BROWSER", "firefox").lower()
# Set HOUSE_HUNT_HEADLESS=0 to run a VISIBLE browser — lets you solve a portal's
# anti-bot challenge (e.g. Leboncoin/DataDome) once; the cookie is then stored in
# the persistent profile and reused (even headless) afterwards.
HEADLESS = os.environ.get("HOUSE_HUNT_HEADLESS", "1").lower() not in ("0", "false", "no")
# Persistent browser profile (keeps cookies across runs).
BROWSER_PROFILE_DIR = os.environ.get(
    "HOUSE_HUNT_PROFILE_DIR", os.path.join(_REPO_ROOT, ".browser-profile"))

# --- Caching ---------------------------------------------------------------
# Per-source TTL in seconds. Public datasets refresh slowly.
_DAY = 86400
CACHE_TTL = {
    "ban": 30 * _DAY,
    "rnb": 7 * _DAY,
    "cadastre": 30 * _DAY,
    "ademe": 7 * _DAY,
    "dvf": 7 * _DAY,
    "georisques": 30 * _DAY,
    "bdnb": 7 * _DAY,
    "listing": _DAY,
    # AI calls are content-addressed (keyed by a hash of their input), so the
    # entry is valid as long as the input is unchanged — keep it effectively
    # forever; a changed page produces a new key and re-runs the call.
    "ai_summary": 3650 * _DAY,
    "ai_structure": 3650 * _DAY,
    "search": 3 * _DAY,
}
DEFAULT_TTL = _DAY
CACHE_DIR = os.environ.get(
    "HOUSE_HUNT_CACHE_DIR", os.path.join(_REPO_ROOT, ".cache"))

# --- Concurrency caps (bounds simultaneous in-flight requests per source) --
CONCURRENCY = {
    "ban": 6,
    "cadastre": 6,
    "ademe": 4,        # ADEME allows ~10 req/s
    "dvf": 6,
    "georisques": 6,
}
DEFAULT_CONCURRENCY = 6
