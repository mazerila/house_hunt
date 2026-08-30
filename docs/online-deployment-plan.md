# Online deployment — analysis & plan

Status: **draft / research** · Date: 2026-08-30 · Owner: Alireza

Goal: run House Hunt as an internet-hosted, multi-user app (own profile, personal
brouillons / annonces / criteria checks) so it's reachable from anywhere, without
exposing the private Claude account to unknown users. Today it runs on a local
`http.server` with no auth, reached over LAN or Tailscale.

---

## 1. The core framing: two planes, not one

| Plane | What it is | Goes online? |
|---|---|---|
| **Control plane** | The dashboard: drafts, dossiers, criteria checks, town sheets, photos, filters, offers | **Yes** — ordinary web app, Firebase fits it well |
| **Research plane** | Claude Code + subagents + the owner's Claude account running the parcel due-diligence checklist and writing `private/listings/<slug>.md` | **No** — not a web service; must stay on the owner's machine |

Almost all the difficulty comes from conflating these. Keep them separate:
put the control plane on Firebase, keep the research plane exactly where it is,
build **one thin bridge** between them (a publish step + a request queue).

---

## 2. Control plane on Firebase (the straightforward 80%)

### 2.1 Auth
- Firebase Auth: Google, Apple, email/password — all native.
- **Manual approval gate** (this is the cost/abuse control): new signups land
  `status: 'pending'`; owner flips to `active` from a one-screen admin view or the
  Firestore console.
- Costs / friction to plan for:
  - Apple sign-in requires an Apple Developer account ($99/yr).
  - Google needs an OAuth consent screen.
  - Email needs a verification flow.

### 2.2 Database — yes, one is required
Cannot have N users editing one `dashboard_db.json`. Firestore fits because the
records are already JSON-shaped documents.

Data-ownership model:

| Scope | Data | Writer |
|---|---|---|
| **Shared, read-only** | `towns/*.md`, `criteria.md` (template), `price_history.json`, methodology | CI/CD (admin SDK) |
| **Per-user** | drafts, dossiers, `criteria_state`, offers (`price_offer` / `price_min`), photos, doc PDFs | the user |
| **Admin (owner)** | user status, the analysis queue | owner |

Firestore layout (proposed):

```
users/{uid}                         { email, status: pending|active|disabled, role }
users/{uid}/drafts/{id}             (brouillons — same fields as today's drafts[])
users/{uid}/listings/{slug}         structured fields + `markdown` blob + `overrides` map
users/{uid}/criteriaState/{listingId}
shared/towns/{id}
shared/criteria                     (the criteria.md template, parsed)
shared/priceHistory
analysisRequests/{id}               { uid, payload, status: requested|running|done, resultRef }
```

Security rules sketch (test with the emulator — multi-tenant rules are where bugs ship):

```
function isActive() {
  return get(/databases/$(database)/documents/users/$(request.auth.uid)).data.status == 'active';
}
match /users/{uid}/{doc=**} {
  allow read, write: if request.auth.uid == uid && isActive();
}
match /shared/{doc=**} {
  allow read: if isActive();
  allow write: if false;              // CI writes via admin SDK, bypasses rules
}
match /analysisRequests/{id} {
  allow create: if isActive() && request.resource.data.uid == request.auth.uid;
  allow read:   if isActive() && resource.data.uid == request.auth.uid;
  allow update, delete: if false;     // owner's local tooling updates status via admin SDK
}
```

Carry over the current **`overrides`** concept (heuristic-derived vs user-edited
fields) — it's already how `dashboard.py` protects `price` / `surface` /
`land_surface` / `agency` / `contact` after a manual edit.

### 2.3 Photos / PDFs
- Firebase Storage under `users/{uid}/listings/{slug}/photos/…` and `/docs/…`.
- Storage rules mirror the Firestore per-user rule.
- The local `sips` resize for `cover.jpg` moves **client-side** (canvas) or into a
  Cloud Function.
- Seller PDFs: Storage + a Firestore doc listing them; the in-page PDF viewer
  keeps working. `doc_title()` keyword rules can run client-side.
- Add per-user quotas — users uploading large HEICs is the obvious abuse.

### 2.4 Hosting + CI/CD
- Firebase Hosting serves `dashboard.html` as a static SPA.
- GitHub Actions on merge to `main`:
  - `firebase deploy --only hosting,functions`
  - a **seed step** that parses `towns/*.md` + `criteria.md` + `price_history.json`
    into `shared/*` (admin SDK).
- "New city" workflow becomes: PR editing `towns/`, merge, CI redeploys +
  reseeds. This is genuinely nicer than the current local edit.

### 2.5 App wiring changes
- The `/api/*` fetches in `dashboard.html` get repointed at the Firestore SDK, or
  at a small set of callable functions.
- **Proxy the enrichment APIs through one Cloud Function** (do not call from the
  browser):
  - fixes CORS (apicarto, geo-DVF S3, georisques),
  - centralises the `house-hunt/0.1` UA and the PII rule,
  - lets you throttle — RNB is 20 req/IP/s and a shared server IP pooling all
    users will get rate-limited without per-user throttling.
- Keep functions to ~3:
  - `onUserCreate` → write `users/{uid}` with `status: 'pending'`
  - `enrich` (callable) → port of `scripts/enrich.py` for one listing + the API proxy
  - `adminSetStatus` → the approve/disable button

---

## 3. Research plane — the hard part

### 3.1 Options

| Option | How | Verdict |
|---|---|---|
| **A. Analysis stays local, results sync up** | Owner runs Claude Code as today. A `publish_dossier.py` writes the finished `.md` + photos to Firestore under the requesting user. Users submit requests into a queue the owner drains offline. | **Recommended.** Zero account exposure. CI/CD untouched. Fits a small, hand-approved pool. |
| **B. Server-side, Anthropic API** | Port the subagent checklist to the Agent SDK on Cloud Run, using an **API key** (not the subscription), billed per token. Per-user quotas or bring-your-own-key. | Real feature, real work. Do later, if demand proven. |
| **C. Bring-your-own-key** | User pastes their own Anthropic API key; their bill. | Niche add-on to A — most users won't have one. |
| **D. Manual user gate** | Approve users by hand; trusted pool only. | Not an alternative — it's the precondition that makes A safe. Use A+D. |

### 3.2 Hard rule
**Do not serve analysis off the Claude Pro / Claude Code subscription.** It's
licensed for individual interactive use; serving other people's requests from it
is against those terms, risks the account, and gives no per-user accounting or
rate control. Server automation is what the **Anthropic API** is for (separate
key, separate billing).

Running Claude Code **inside a GitHub Action** does not change this — it still
needs an API key, still costs per run, so it still needs the manual gate. Same
conclusion.

### 3.3 Recommended: A + D
Approved users get the dashboard and all shared knowledge (towns, criteria, price
history, methodology). Auto-analysis stays the owner, offline, on request,
published back to the user's profile. If it takes off, add B as a funded tier.

### 3.4 The bridge (only coupling between the two planes)
Two small local scripts, using the Firebase admin SDK:
- `scripts/pull_requests.py` — list `analysisRequests` where `status == 'requested'`.
- `scripts/publish_dossier.py` — push `private/listings/<slug>.md` + `photos/` to a
  target `uid` (or a shared library), set `status: 'done'`, link `resultRef`.

Local `private/` stays the owner's source of truth and git history. The deployed
per-user data is separate. The publish step is one-way (local → cloud) to avoid
two-sources-of-truth conflicts.

---

## 4. Recommended architecture (concrete)

- **Firebase Hosting** → static `dashboard.html` + assets
- **Firebase Auth** → Google + Apple + email
- **Firestore** → layout in §2.2, rules gated on `status == 'active'`, region `eur3`
- **Firebase Storage** → per-user photos/docs
- **Cloud Functions** → `onUserCreate`, `enrich` (+ API proxy), `adminSetStatus`
- **GitHub Actions** → deploy on merge; seed `shared/*` when `towns/` /
  `criteria.md` / `price_history.json` change
- **Local, unchanged** → the Claude Code loop over `private/`, plus
  `pull_requests.py` + `publish_dossier.py`

Plan (Firebase Blaze — pay-as-you-go — is required for Cloud Functions with
outbound network). Cost at a handful of users is near-zero; still set a **budget
alert**.

---

## 5. Phased plan

- [ ] **Phase 0 — freeze the data-ownership split** (§2.2 table).
- [ ] **Phase 1 — app refactor**: records md-files → Firestore; carry `overrides`;
      repoint `/api/*`. Keep md as the owner's local export.
- [ ] **Phase 2 — auth + user management**: Firebase Auth, `users/{uid}` doc,
      `pending → active` flow, security rules, emulator tests.
      *(Stop here and "me + partner, from anywhere" already works, analysis still
      done locally as today.)*
- [ ] **Phase 3 — storage**: photos/docs, client-side resize, PDF viewer.
- [ ] **Phase 4 — shared-knowledge sync via CI**: seed script for towns /
      criteria / price history.
- [ ] **Phase 5 — analysis queue**: `analysisRequests` collection, the two bridge
      scripts, a "Demander l'analyse" button on drafts.

---

## 6. What NOT to do

- **Don't** run analysis on the Claude subscription for other users — API key,
  server-side, or not at all.
- **Don't** put any key (Anthropic, mail app-password) in the client or the repo.
  Client keys are extractable. Secret Manager / function env only.
- **Don't** call enrichment APIs straight from browsers — CORS + rate limits +
  the PII/UA rule all argue for one server proxy.
- **Don't** seed existing `private/` dossiers into the shared multi-tenant DB.
  That's the owner's search; publish per-user, deliberately.
- **Don't** let users edit `towns/` or `criteria.md` — those are curated
  knowledge; give a personal overlay (`criteria_state` already is one).
- **Don't** auto-approve signups — the manual gate is the whole cost/abuse story.
- **Don't** build a relational backend if Firestore + rules cover it. Keep the
  stdlib-simple spirit of the current app.
- **Don't** ignore GDPR once strangers have accounts — privacy policy,
  delete-on-request, EU Firestore region. Auth = you're a data controller.
- **Don't** lose the CLAUDE.md PII rule — now it's *multiple* users' identities
  that must never reach the open APIs; the proxy enforces the generic UA.

---

## 7. Challenges

- **Data migration** md → Firestore, preserving the derived/user-owned field
  split and `overrides`.
- **Porting `enrich.py`** to a function — geocodes + calls apicarto/georisques;
  moderate. `enrich_towns.py` can stay a CI batch job (shared data).
- **Two sources of truth** — local `private/` and cloud Firestore; keep sync
  one-way.
- **Rules correctness** — multi-tenant isolation is easy to get subtly wrong.
- **Losing buildless simplicity** — Firebase SDK, maybe a build step. Can stay
  buildless with the compat SDK from a CDN, but it's a shift from "one Python
  file, stdlib only".
- **Ops burden** — uptime, backups, "why is my analysis 3 days late", lost data.
- **Apple sign-in + OAuth consent** setup friction.
- **Blaze billing** — needs a billing account + budget alert.

---

## 8. Pros / cons

**Pros**
- Access anywhere, no Tailscale; shareable with partner/friends.
- Real backup — today a dead laptop loses `dashboard_db.json`.
- CI/CD for towns/criteria nicer than local edits.
- Manual approval is a simple, robust gate.
- Free/low tier likely covers this scale.

**Cons**
- Meaningful ops burden for a currently-personal tool.
- The analysis — the actual value — stays manual and bottlenecked on the owner;
  it doesn't scale with users.
- Firebase lock-in (Firestore model, rules DSL, Auth); later migration is work.
- Privacy/GDPR surface once strangers sign up.
- Low but non-zero cost that needs monitoring.
- Two sources of truth to keep disciplined.

---

## 9. Alternatives to weigh first

- **Just me + 1–2 trusted people:** Tailscale already gives *you* access
  anywhere. Add a shared login (or Tailscale Serve + per-device auth) and a
  nightly `dashboard_db.json` backup to a private repo/bucket. No rebuild. This
  covers "access from anywhere" without multi-tenant work.
- **Supabase instead of Firebase:** Postgres + row-level security + auth
  (Google/Apple/email) + storage, EU-hosted, less lock-in, SQL-native.
  Comparable effort; better if relational + SQL is preferred over a document
  store and a rules DSL.
- **Self-host + auth proxy:** current `dashboard.py` behind Caddy + an OAuth2
  proxy on a cheap VPS. Still needs the DB change for real multi-user, but keeps
  the Python app and avoids Firebase entirely.

**Honest read:** the multi-user rebuild is worth it only if several *unrelated*
people will use it. If the real need is "me and my partner, from anywhere", the
Tailscale + shared-login + backup path gets there in a week with none of the
downsides.
