# house-hunt

A methodology + toolkit for doing **serious due diligence on French house listings** — houses, terrains, teardown plots — built around France's open geo/property data. It grew out of a real house search in Hauts-de-Seine and is designed to be driven by an AI agent (Claude Code) or used as plain CLI scripts.

For any listing it can:

- **Locate the exact cadastral parcel** from an address, coordinates, or a fuzzy "section X, ~N m²" description (BAN geocoding, apicarto cadastre, RNB building registry).
- **Pull the planning red flags** at the parcel: PLU zone, OAP/EBC/EPP prescriptions, quarry (carrières) and ABF heritage servitudes (apicarto GPU).
- **Fingerprint and harvest the DPE** (ADEME registry) — including locating withheld-address listings and cross-anchoring them to a building via the RNB id, then extracting the full works map (envelope, heating, surfaces).
- **Read the market**: the parcel's own past sales and same-street comps from DVF.
- **Check risks**: clay shrink-swell, quarries, ground movement, BASIAS/BASOL (Géorisques).
- **Reconcile the seller's own documents** — DDT (diagnostics), certificat Carrez, audit énergétique, agency ebook — against the ad and the public registries, and re-price the listing on what they actually say.
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

## Dashboard (local GUI)

A local, French-language dashboard (responsive — works on phone and desktop) to browse and track every listing you've researched. 100% offline-local: Python stdlib only, no external resources, bound to `127.0.0.1` by default; its "database" is just `private/dashboard_db.json` (gitignored, never committed).

```sh
python3 scripts/dashboard.py            # default port 8420, this machine only
# then open http://localhost:8420

# to reach it from your phone / another device on the same Wi-Fi:
python3 scripts/dashboard.py --lan      # binds 0.0.0.0; prints your http://192.168.x.x:8420 URL
```

`--lan` (or `--host 0.0.0.0`) exposes the dashboard to your local network — anyone on the same Wi-Fi can open it and it has no password, so use it only on a trusted network. On macOS the first run may pop a firewall prompt ("allow incoming connections") — accept it, and make sure both devices are on the same network.

How to use:

1. **Research listings** (with the Claude Code checklist or by hand) so notes land as markdown files in `private/listings/<slug>.md`.
2. **Open the dashboard** — on startup it imports every note into the grid. Click **🔄 Réimporter** anytime to pick up new/edited notes.
3. **Filter & sort** — text search, status, commune, minimum rating on top; click any column header (Prix, Terrain, Surface, €/m², DPE, Note…) to sort.
4. **Track progress** — change a listing's status directly from the grid pill (Recherche → Visite prévue → Visité → Offre / Rejeté); it saves immediately.
5. **Open a row** for details: rate it (★), tag it, write a verdict/comment, set the asking / minimum / offer prices, and read the full research note rendered inline. Prix / Terrain / Surface are editable — a hand-typed value takes priority and re-imports never overwrite it (clear the field to hand it back to the extractor). A row can be removed with **Retirer cette annonce** (hides it from the dashboard; the `.md` file stays on disk and re-import won't bring it back).
6. **Photos & videos** — drop media into the listing's folder `private/listings/<slug>/photos/` (or the folder itself); they appear as a **Photos** gallery at the top of the drawer. Click any to open full-size: **← → / Esc**, or **swipe left/right** (touch or mouse-drag) between images. Videos play in the viewer with controls.
   - Supported: `jpg png webp gif heic/heif` (HEIC is transcoded to JPEG on the fly so any browser shows it) and `mp4 mov webm m4v`. **Caveat:** iPhone `.mov` files are usually **HEVC/H.265**, which plays in Safari but not Chrome/Firefox — convert those to MP4/H.264 for cross-browser playback.
   - **Annonces** — at the bottom of the drawer, paste the listing's online announce URL(s); any URLs already in the research note are detected automatically. All are clickable.
7. **Documents (PDF)** — drop the seller's PDFs (DDT/diagnostics, DPE, certificat Carrez, audit énergétique, taxe foncière, ebook agence…) into the listing's folder `private/listings/<slug>/` (or a `docs/` subfolder). They appear as a **Documents** list under the photos — click one to read it in a built-in viewer (**‹ ›** between documents, **Esc** to close, plus *Nouvel onglet* / *Télécharger*). No naming convention needed: sellers' filenames are cryptic, so each row shows a **readable title** derived from the filename (`DDT_-_LDI-26-3982-DUPONT.pdf` → *Dossier de diagnostic technique (DDT)*) with the real filename underneath. Unrecognised names simply show as-is.
8. **Columns** — the **Colonnes** button picks which columns show; the choice is saved server-side (shared, persists across restarts). Every listing gets a short sortable **code** (H01, H02, …) for easy reference.
9. **Themes** — the ◐ button toggles dark/light; your choice is remembered.
10. **Network access & mobile** — see `--lan` above to open it from your phone; on small screens the grid collapses into one card per listing for comfortable browsing.

Extracted fields (price, surfaces, DPE, commune) come from heuristics over your notes — correct any misread in the drawer once, it sticks.

## The methodology

The real value is in [CLAUDE.md](CLAUDE.md): a battle-tested **8-step parcel due-diligence checklist** (locate → planning layers → slope → frontage → DVF → DPE → permits → verdict), the pitfalls that produced it, all-in cost rules of thumb, and a hard-nosed section on French self-build (auto-construction) insurance/exit risks.

It also carries a **seller-document reconciliation pass** for the moment an owner or agency finally hands over the paperwork. Those PDFs are the first contractual facts in the file, and they tend to contradict the ad in the seller's favour — so the checklist forces you to re-derive the surface from the **certificat Carrez** (adverts often quote *surface au sol*, not habitable, and that alone can move €/m² onto the local median), pull the **full ADEME record** from the DPE number in the DDT rather than trusting the advertised letter (it's where the real heating system shows up), read the **électricité** anomalies and the **assainissement** observations as costed works, treat the agency's own **ebook** condition grid as more honest than its portal ad, note what's **missing** (an absent ERP hides the clay/quarry risk), and then restate — not quietly delete — any earlier conclusion the documents overturn.

`CLAUDE.md` doubles as the instruction file when you open this repo in [Claude Code](https://claude.com/claude-code): the agent reads it and runs the checklist for a listing you paste in, using the scripts and open APIs below.

## Layout

- `scripts/` — the toolkit. Notable entry points: `rnb_lookup.py` (address → building/parcels), `ademe.py` (DPE registry queries), `cadastre.py`, `georisques.py`, `compare_dpe.py` (DPE XML diff), `listing.py` / `search.py` (portal scraping + AI extraction), `dashboard.py` (local GUI server; serves `scripts/dashboard.html`). Shared plumbing: `httpclient.py` (rate-limited async HTTP), `cache.py` (on-disk TTL cache), `settings.py` (all endpoints/config).
- `private/` — **gitignored.** Your own investigation data: per-listing folders, notes, verdicts (`private/list.md`), downloaded records. The methodology writes here; nothing in it is ever published.

## Privacy stance

The toolkit sends **no personal identifiers** to third-party APIs by default: generic User-Agent, no contact email unless you opt in via `HOUSE_HUNT_FROM_EMAIL`. See the privacy section of `CLAUDE.md`.

## License

MIT — see [LICENSE](LICENSE).
