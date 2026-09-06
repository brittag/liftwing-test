# Projo

Flask prototype with four tools:

1. **Articles to improve** (`/`) — placeholder for a future ranking tool
2. **Tone Checker** (`/tone`) — paste titles for on-demand tone-check flag counts
3. **Articles to review** (`/review`) — ranked list of unreviewed English Wikipedia articles
4. **Orphan linker** (`/orphan`) — placeholder for a future linking tool

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with Toolforge replica credentials
```

## Refresh the queue snapshot

The UI reads `data/npp_queue.json`. On Toolforge this file is kept current by
three scheduled jobs (see `jobs.yaml` — load later with
`toolforge jobs load jobs.yaml`, not done yet):

| Cadence | Command | What it does |
|---------|---------|----------------|
| Hourly | `--mode prune` | Drop pages that have been reviewed, deleted, redirected, or left PageTriage |
| Daily | `--mode ingest` | Prune, then add up to 2000 newest unreviewed pages not already in the snapshot |
| Weekly | `--mode refresh-existing` | Re-scan SQL flags for remaining pages; Lift Wing / ChatGPT only if the revision changed; refresh pageviews and creator blocks |

Ingest and weekly **do not** re-score the whole backlog on every run. Existing
rows keep cached `reference_need` until their `revision_id` changes.

**Locally**, open an SSH tunnel to the enwiki analytics replica first (leave it
running). On Toolforge, jobs connect to the replica directly (`replica.my.cnf`
or `TOOL_REPLICA_USER` / `TOOL_REPLICA_PASSWORD`).

```bash
ssh -L ${TOOLFORGE_DB_PORT:-3307}:enwiki.analytics.db.svc.wikimedia.cloud:3306 \
  ${TOOLFORGE_USER}@login.toolforge.org -N
```

Or use the helper from the database skill if you have it wired up.

```bash
# Cadence jobs (same as Toolforge; need the replica)
python scripts/refresh_npp_queue.py --mode prune
python scripts/refresh_npp_queue.py --mode ingest            # newest 2000
python scripts/refresh_npp_queue.py --mode ingest --limit 50
python scripts/refresh_npp_queue.py --mode refresh-existing --sql-only

# One-shot / local testing
python scripts/refresh_npp_queue.py --api --limit 50
python scripts/refresh_npp_queue.py --limit 50
python scripts/refresh_npp_queue.py --sql-only
python scripts/refresh_npp_queue.py --sql-only --oldest --merge --limit 500
python scripts/refresh_npp_queue.py --refresh-pageviews

# Full backlog (first Lift Wing pass is slow — ~8 hours at 1s throttle)
python scripts/refresh_npp_queue.py
```

Scoring factors (tunable in `app/npp_config.py`): reference need, pageviews,
page length (0–10), age &gt;90 days (+5), AI / ChatGPT citation markers
(`utm_source=chatgpt.com`) / notability / COI / promotional tags, living people
& minors, contentious topics, orphans, blocked registered creators (+10),
NPR/AfC reviewer creators (−10), accepted AfC submissions (−10; talk page in Accepted AfC submissions), POV disputes
(+10), violence-related categories (+20; films excluded via `*_films` /
`Films_*`), sports (−5; sportspeople/players plus tennis/football/soccer/
handball/sailing); disambiguation pages get a −20 penalty.

On **Articles to review**, the **Rescore** zippy exposes every one of these
weights (plus the pageview/page-length log caps and stale-day threshold) as
editable fields. Editing one re-ranks the snapshot live; "Reset to defaults"
restores the snapshot's scores and "Copy as JSON" copies the current parameter
set for pasting into `npp_config.py`. Under the hood this hits
`GET /api/queue?w_<param>=<number>` (defaults from `GET /api/scoring-params`).

CTOP article-side matching uses a precomputed subcategory list (tight roots
only). Rebuild it with:

```bash
python scripts/expand_ctop_categories.py
```

NPR + AfC reviewer usernames (for the −10 creator discount) live in
`data/npp_afc_reviewers.json`. Rebuild with:

```bash
python scripts/fetch_npp_afc_reviewers.py
```

## Run the web app

```bash
python app/app.py
```

Open http://localhost:8765 for Articles to improve,
http://localhost:8765/tone for Tone Checker,
http://localhost:8765/review for Articles to review, or
http://localhost:8765/orphan for Orphan linker.
