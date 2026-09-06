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

The UI reads `data/npp_queue.json`. Build or update it with the refresh job
(requires an SSH tunnel to the enwiki analytics replica).

**1. Open the tunnel** (leave this running):

```bash
ssh -L ${TOOLFORGE_DB_PORT:-3307}:enwiki.analytics.db.svc.wikimedia.cloud:3306 \
  ${TOOLFORGE_USER}@login.toolforge.org -N
```

Or use the helper from the database skill if you have it wired up.

**2. Run the refresh:**

```bash
# No SQL yet? Test 50 unreviewed pages via public APIs + Lift Wing:
python scripts/refresh_npp_queue.py --api --limit 50

# With replica tunnel + .env — same test via SQL:
python scripts/refresh_npp_queue.py --limit 50

# SQL flags only (reuse cached reference_need; minutes for full backlog)
python scripts/refresh_npp_queue.py --sql-only

# Add 500 oldest unreviewed pages into the existing snapshot (no replacements)
python scripts/refresh_npp_queue.py --sql-only --oldest --merge --limit 500

# Fill/refresh pageviews via REST API (day-cached; page_props is unused on replicas)
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
