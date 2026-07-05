# House Hunt

A methodology + toolkit for doing serious due diligence on French house listings (houses, terrains, teardown plots), developed during a real search in Hauts-de-Seine. The examples and hard-learned lessons below come from actual listings vetted with it.

Your own investigation data (per-listing notes, verdicts, downloaded records) lives under `private/` — gitignored, never published. See `private/README.md` for the layout.

## Privacy — no PII to third parties

Never send the user's personal identifiers (name, private email, phone, employer, exact current address) to third-party or open APIs — not in User-Agents, not in query params (e.g. the RNB `from=`), not in webhooks, and not embedded in web-search queries or fetched URLs. Concretely:

- Outbound requests use the **generic UA** `house-hunt/0.1`. A contact email is included **only** if `HOUSE_HUNT_FROM_EMAIL` is explicitly set in the gitignored `.env` (opt-in, never hardcoded in source — this repo previously leaked a private email as a hardcoded default; don't reintroduce one).
- Credentials for the user's own accounts (API keys, mail app-passwords) live **only** in the gitignored `.env`, are sent only to their own provider, and are never printed, logged, cached, or committed.
- Public-record data _about properties_ (cadastre, DVF, permits, seller SIREN) is fine to query and store; the rule is about the **user's** identity travelling outward.
- When drafting outbound artifacts (emails to agents, forms), leave identity fields as placeholders (`[Prénom NOM]`) unless the user explicitly says to fill them.

## Tools

These tools can be used by automation and interactively to research different listings.

### Meta Search engine

https://moteurimmo.fr/

This site can be used to search multiple sites at the same time.

### cartes.gouv.fr

URL: https://cartes.gouv.fr/explorer-les-cartes/

### RNB — Référentiel National des Bâtiments

- Docs: https://rnb-fr.gitbook.io/documentation
- Map: https://rnb.beta.gouv.fr/carte
- API base: https://rnb-api.beta.gouv.fr/api/alpha/buildings/

The RNB is the French national building registry: it gives every building one **unique, permanent ID-RNB** and acts as a _pivot identifier_ to cross-reference 38+ databases — including **DPE**, **fichiers fonciers**, **cadastre / parcelles**, **BD TOPO (IGN)**, **BDNB**, and the **registre des copropriétés**. Open data under licence Etalab 2.0. (APIs are alpha — request/response formats may change; the IDs are stable.)

- **ID-RNB:** 12 characters in 3 groups of 4 (alphanumeric, case-insensitive, non-significant — carries no commune code). Example: `NHDE-2W8H-E3X3`.
- **Map UI:** search an address or click a building to read its RNB ID, status, addresses and linked parcels.
- **Buildings API:** consult a building by RNB ID, search by location / address / cadastral parcel, list by INSEE code, or get a differential since a date. Returns a point + footprint (WGS 84), status (`constructed` / `notUsable` / `demolished`), BAN address IDs, cadastral parcels, and `ext_ids` linking BD TOPO / BDNB. Rate-limited to 20 req/IP/s; no key required (add `?from=you@example.com` to be notified of API changes).
- **Other tools:** vector tiles, ADS API (permits / autorisations du droit des sols), national & departmental export (full DB download), OGC API Features, and a sandbox.

**Why it's useful here:** the DPE XML carries an `id_batiment_rnb` field — resolving a listing's building in the RNB ties it to its **parcels** (e.g. F 299 / F 300) and pulls cross-referenced **DPE / fichiers fonciers** records for the same building.

---

## Parcel due diligence — required checks

Run all of these for every listing. Cite the source for each fact, and keep **data facts separate from on-the-ground reality** (the cadastre can lie about frontage).

1. **Locate the exact parcel.**
   - Coordinates (convert DMS → decimal) → apicarto cadastre _point-in-parcel_.
   - Address → `scripts/rnb_lookup.py "<addr>" --print` (or BAN geocode).
   - Fuzzy ("section X, ~N m²") → apicarto _section scan_, **via `curl` on the full JSON + local filter** — a WebFetch summary truncates and silently drops parcels (it missed the real match on Antony section CM).
   - Confirm cadastral `contenance` ≈ listed surface; a >~5 m² gap ⇒ possible division-in-progress.
   - **Ambiguity gate:** if several parcels match _and they fall in different zones_, do NOT commit to one — flag it and ask for the exact pin. (Antony CM straddles U1g\* pavillonnaire **and** the Antonypôle OAP; guessing the parcel flips the verdict between candidate and reject.)

2. **Planning layers** — apicarto GPU at the parcel point → the red flags:
   `zone-urba` (zone + `E/T/H/A` indices), `prescription-surf` (**EPP/EBC/OAP**, emplacement réservé, gare-perimeter), `assiette-sup-s` (**carrières PM1**, **ABF AC1 abords MH**).
   Also sample the **adjacent parcels' zone/prescriptions** — an OAP/gare sector means you vet _this_ plot but inherit years of construction and densifying (collective/social-housing) neighbours around it.

3. **Slope** — always check elevation via **IGN RGE ALTI** and compute the % grade over the plot depth. A listing touting "sous-sol total" is usually signalling a slope. Slope = costs more to build **and** sells for less.

4. **Frontage / geometry** — derive it from the parcel vertices but **treat it as provisional**: never conclude "not a flag-lot" from the cadastre alone; always flag it for Street View + the bornage. (Fontenay P218 read as a normal ~10 m front on the cadastre; on the ground it was a narrow back-lot.)

5. **DVF** — geo-DVF S3 CSV: the parcel's own sale (= seller cost basis / negotiation anchor) + same-street comps (= value read).

6. **DPE registry (ADEME) — mandatory for any _house/apartment_ listing** (n/a for bare terrain). Two uses:
   - **Locate** a withheld-address listing by fingerprint: query `etiquette_dpe` + `type_batiment` + commune, then match build year / energy / surface. **Use WIDE surface bands and match on several traits** — advertised and DPE surfaces disagree routinely (Bagneux: ad said 165 m², DPE 194 m² → a 150–180 filter missed the real house and matched the wrong one). Cross-check against the listing's map/street clues; the ambiguity gate applies here too.
   - **Confirm + harvest — always pull the FULL record** (by `numero_dpe` or address) once a candidate exists, _before_ the verdict: it carries **`id_rnb`** (cross-anchor with the RNB building = identity proof), the **true surface habitable**, `periode_construction`, the exact **heating/ECS systems and their ages**, the **envelope scorecard** (walls/windows/VMC/confort d'été = the works map), annual cost estimate, and validity dates. A fresh DPE date also reveals when the seller started preparing the sale.

7. **Permits** — France Cadastre / basedespermis for a granted PC or permis de démolir on _this_ parcel: formal number, applicant (private vs _marchand_), dates, and whether **purgé de tout recours**.

8. **Verdict** — buildability + **all-in price** + **self-build fit**; then update `private/list.md` (`## Active` / `## Rejected`, one-line reason). Price the **serviced, cleared** plot, not just the land: add **viabilisation/raccordement €5–30k** (distance-to-networks driven), **taxe d'aménagement 5–8%**, any **demolition**, and a **25–30% build contingency** (self-builds overrun) — plus double-carry (current housing + loan interest) over a **32–48-month** build.

**Buyer filters** (an example profile — adapt to your own constraints): self-buildable (not CCMI / "loi 19-12-1990"-locked), flat, wide frontage, no carrières if avoidable, outside a dense OAP, ≤ ~30 min from [your anchor point — work/school/family], low social housing. **No heritage-locked properties** (ABF abords / ensemble bâti remarquable / EPP on the building): unmodifiable + thin resale = unacceptable illiquidity (the Cherrier lesson — long time-on-market is the exit preview).

**Two lanes:** a granted + purgé + _transferable_ PC de-risks buildability but carries a ~€60–100k premium (anchor on the seller's DVF cost basis; don't pay it twice). A clean raw/teardown plot lets you file **your own** PC — via a _condition suspensive d'obtention du PC_, or a positive **CUb** that freezes the rules 18 months — and design your own house: the better lane when you're patient.

## Analysis tools & endpoints

Repo scripts (use `./.venv/bin/python`): `scripts/rnb_lookup.py "<address>" --print` (address → RNB buildings + parcels), `scripts/compare_dpe.py <a.xml> <b.xml>` (DPE 3CL XML diff).

Open APIs — no key; `curl` or WebFetch. `geom` is a URL-encoded GeoJSON `{"type":"Point","coordinates":[LON,LAT]}` (**[lon, lat]**):

- **Cadastre / geometry:** `apicarto.ign.fr/api/cadastre/parcelle` — `?geom=<Point>`, `?code_insee=&section=&numero=`, or `?code_insee=&section=` (whole section). Empty at a point = the pin is on the street.
- **PLU/PLUi (zone + red flags):** `apicarto.ign.fr/api/gpu/{zone-urba,prescription-surf,assiette-sup-s}?geom=<Point>`.
- **Geocode / reverse:** `api-adresse.data.gouv.fr/search/?q=…` · `…/reverse/?lon=&lat=`.
- **Elevation / slope:** IGN RGE ALTI (Géoplateau altimétrie) — sample points along the plot to get the grade.
- **DVF (sales):** `https://geo-dvf.s3.sbg.io.cloud.ovh.net/latest/csv/<YEAR>/communes/<DEPT>/<INSEE>.csv` — the `files.data.gouv.fr` URL 302-redirects here, so hit the S3 URL directly; filter by `id_parcelle` / `adresse_nom_voie`.
- **DPE registry (ADEME):** `data.ademe.fr/data-fair/api/v1/datasets/meg-83tjwtg8dyz4vv7h1dqe/lines?size=…&qs=<lucene>` — qs supports `code_postal_ban:92220 AND etiquette_dpe:C AND type_batiment:maison AND surface_habitable_logement:[150 TO 200]` and `numero_dpe:"…"`; omit `select` to get the full record (~200 fields incl. `id_rnb`). NB: dense communes have >10k DPEs — always filter server-side, never page through.
- **Permits:** `france-cadastre.fr/permisdeconstruire/<commune>` and `basedespermis.fr/…` (HTML → WebFetch). PC number `PC 0<INSEE> <YY> <NNNNN>`; granted + decision >~5 months old ⇒ purgé.
- **Risks:** Géorisques (carrières, RGA argiles, mouvement de terrain, BASIAS/BASOL) + the ERP.

Listing portals (PAP, leboncoin, SeLoger, Bien'ici, moteurimmo) block automated fetches — work from the pasted text.

## Self-build — insurance, cost & the human risks

Beyond any single parcel, these decide whether the project _survives_ (French auto-construction reality):

- **Insurance-first, or you can't resell.** You essentially can't get _décennale_ on your own labour, and without it no _dommages-ouvrage_ (mandatory since the loi Spinetta). For **10 years** you remain the legal _constructeur_ (liable to buyers for structural defects), and selling within those 10 years means the notaire flags the missing DO → buyers discount hard or can't get financing. **Mitigation: a hybrid build — insured pros carry the décennale on structure / envelope / RE2020 systems (so DO is obtainable), DIY only the low-risk finishes.** Look into self-build insurers, the _Castors_ model, or a partial DO.
- **All-in, illiquid, overrun-prone money.** Land + a half-built house is nearly unsellable across the ~**32-month** (range 12–84) build → **no exit** if life changes. Budget **25–30% contingency**, viabilisation, taxe d'aménagement, **TVA 20% on materials** (no recovery for an owner-occupier), and double-carry; there is **no CCMI price/delivery guarantee** — you carry overrun and delay risk yourself.
- **Set written exit gates _before_ committing** (e.g. "stop and sell the permitted land if not weathertight by month X, or if the G2 / insurance comes back bad"). The sunk-cost trap is real once ~€200k and 2 years are in.
- **Standing human note — the forums say this ends more projects than technique does.** The difficulty is _psychological_: burnout after ~2 years, the grind of **living in an unfinished house**, and a documented **couple break-point** (_"on commence en couple, le couple ne résiste pas"_ — the build monopolises attention "comme l'arrivée d'un enfant"). So: **sequence a livable core fast**, plan for **32–48 months** not a smooth 5, and if partnered make it a genuinely shared decision.
