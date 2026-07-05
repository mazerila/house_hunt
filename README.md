# house-hunt

A methodology + toolkit for doing **serious due diligence on French house listings** — houses, terrains, teardown plots — built around France's open geo/property data. It grew out of a real house search in Hauts-de-Seine and is designed to be driven by an AI agent (Claude Code) or used as plain CLI scripts.

For any listing it can:

- **Locate the exact cadastral parcel** from an address, coordinates, or a fuzzy "section X, ~N m²" description (BAN geocoding, apicarto cadastre, RNB building registry).
- **Pull the planning red flags** at the parcel: PLU zone, OAP/EBC/EPP prescriptions, quarry (carrières) and ABF heritage servitudes (apicarto GPU).
- **Fingerprint and harvest the DPE** (ADEME registry) — including locating withheld-address listings and cross-anchoring them to a building via the RNB id, then extracting the full works map (envelope, heating, surfaces).
- **Read the market**: the parcel's own past sales and same-street comps from DVF.
- **Check risks**: clay shrink-swell, quarries, ground movement, BASIAS/BASOL (Géorisques).
- **Scrape and structure listings** from portals with a persistent Playwright browser + Claude extraction.

## Quickstart

Requires Python 3.10 (a `.python-version` for pyenv is included).

```sh
python -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/playwright install firefox   # only needed for listing scraping
cp .env.example .env                     # then fill in what you need
```

Try it:

```sh
# Address -> RNB buildings + cadastral parcels
./.venv/bin/python scripts/rnb_lookup.py "35 avenue du général leclerc, bourg-la-reine" --print

# Diff two DPE XML files field by field
./.venv/bin/python scripts/compare_dpe.py OLD.xml NEW.xml
```

## The methodology

The real value is in [CLAUDE.md](CLAUDE.md): a battle-tested **8-step parcel due-diligence checklist** (locate → planning layers → slope → frontage → DVF → DPE → permits → verdict), the pitfalls that produced it, all-in cost rules of thumb, and a hard-nosed section on French self-build (auto-construction) insurance/exit risks.

`CLAUDE.md` doubles as the instruction file when you open this repo in [Claude Code](https://claude.com/claude-code): the agent reads it and runs the checklist for a listing you paste in, using the scripts and open APIs below.

## Layout

- `scripts/` — the toolkit. Notable entry points: `rnb_lookup.py` (address → building/parcels), `ademe.py` (DPE registry queries), `cadastre.py`, `georisques.py`, `compare_dpe.py` (DPE XML diff), `listing.py` / `search.py` (portal scraping + AI extraction). Shared plumbing: `httpclient.py` (rate-limited async HTTP), `cache.py` (on-disk TTL cache), `settings.py` (all endpoints/config).
- `private/` — **gitignored.** Your own investigation data: per-listing folders, notes, verdicts (`private/list.md`), downloaded records. The methodology writes here; nothing in it is ever published.

## Privacy stance

The toolkit sends **no personal identifiers** to third-party APIs by default: generic User-Agent, no contact email unless you opt in via `HOUSE_HUNT_FROM_EMAIL`. See the privacy section of `CLAUDE.md`.

## License

MIT — see [LICENSE](LICENSE).
