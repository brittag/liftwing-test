#!/usr/bin/env python3
"""Refresh the ranked NPP urgency queue snapshot.

Pulls unreviewed mainspace articles from the enwiki replica, attaches
category / orphan / BLP / CTOP / pageview flags, scans wikitext for ChatGPT
citation markers, checks whether the registered page creator is currently
blocked, scores reference-need via Lift Wing (incrementally), and writes
data/npp_queue.json.

Cadences (for Toolforge jobs):
  python scripts/refresh_npp_queue.py --mode prune             # hourly
  python scripts/refresh_npp_queue.py --mode ingest            # daily
  python scripts/refresh_npp_queue.py --mode refresh-existing  # weekly

One-shot / local:
  python scripts/refresh_npp_queue.py              # full refresh (SQL)
  python scripts/refresh_npp_queue.py --sql-only   # skip Lift Wing
  python scripts/refresh_npp_queue.py --limit 50   # test subset
  python scripts/refresh_npp_queue.py --api --limit 50  # no SQL; Action API
  python scripts/refresh_npp_queue.py --sql-only --oldest --merge --limit 500
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

# Allow imports from app/
ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

from npp_config import (  # noqa: E402
    CAT_AFC_ACCEPTED,
    CAT_AI_PREFIX,
    CAT_COI_PREFIX,
    CAT_CTOP_TALK,
    CAT_DISAMBIG,
    CAT_LIVING_PEOPLE,
    CAT_NOTABILITY,
    CAT_ORPHAN,
    CAT_POV,
    CAT_PROMOTIONAL,
    LANG,
    MINOR_BIRTH_YEAR_END,
    MINOR_BIRTH_YEAR_START,
    PAGEVIEW_LAG_DAYS,
    PAGEVIEW_LOOKBACK_DAYS,
    SNAPSHOT_PATH,
    SPORTS_SUBSTRINGS,
    VIOLENCE_SUBSTRINGS,
    load_ctop_category_map,
    load_reviewer_usernames,
)
from npp_score import (  # noqa: E402
    birth_year_from_categories,
    page_has_violence,
    page_is_sports,
    score_article,
    wikitext_has_chatgpt,
)
from scoring import get_user_agent, make_session, score_reference_need  # noqa: E402

API = f"https://{LANG}.wikipedia.org/w/api.php"
PAGEVIEWS_API = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("refresh_npp_queue")

CHUNK = 2000
PROGRESS_EVERY = 50
API_BATCH = 40
WIKITEXT_BATCH = 20
USERS_BATCH = 50
PAGEVIEWS_INTERVAL = 0.2  # seconds between Pageviews REST calls
INGEST_LIMIT = 2000
MODES = ("prune", "ingest", "refresh-existing")


def _api_get(session, params: dict[str, Any]) -> dict[str, Any]:
    params = {**params, "format": "json", "formatversion": 2}
    backoff = 1.0
    for _ in range(6):
        resp = session.get(API, params=params, timeout=60)
        if resp.status_code == 429:
            time.sleep(float(resp.headers.get("Retry-After", backoff)))
            backoff = min(backoff * 2, 60)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("Too many 429s from Action API")


def fetch_unreviewed_via_api(
    session, limit: int
) -> list[dict[str, Any]]:
    """Pull unreviewed mainspace pages from PageTriage (no SQL)."""
    pages: list[dict[str, Any]] = []
    offset = 0
    # Page through until we have enough non-redirects (front of queue is often redirects).
    while len(pages) < limit and offset < 5000:
        batch_limit = 200
        data = _api_get(
            session,
            {
                "action": "pagetriagelist",
                "showunreviewed": 1,
                "namespace": 0,
                "limit": batch_limit,
                "offset": offset,
            },
        )
        chunk = (data.get("pagetriagelist") or {}).get("pages") or []
        if not chunk:
            break
        for p in chunk:
            if str(p.get("is_redirect", "0")) == "1":
                continue
            try:
                linkcount = int(p.get("linkcount") or 0)
            except (TypeError, ValueError):
                linkcount = 0
            pages.append(
                {
                    "page_id": int(p["pageid"]),
                    "page_title": str(p["title"]).replace(" ", "_"),
                    "page_len": int(p.get("page_len") or 0),
                    "created": str(p.get("creation_date") or ""),
                    "revision_id": None,
                    "avg_daily_views": 0,
                    "_linkcount": linkcount,
                }
            )
            if len(pages) >= limit:
                break
        offset += len(chunk)
        if len(chunk) < batch_limit:
            break
    log.info("PageTriage API returned %d non-redirect pages", len(pages))
    return pages[:limit]


def enrich_via_api(
    session, raw: list[dict[str, Any]]
) -> tuple[
    list[dict[str, Any]],
    dict[int, set[str]],
    set[int],
    set[int],
    dict[int, int],
]:
    """Batch-fetch rev ids, categories, pageviews, linkshere, talk CTOP/AfC."""
    cats: dict[int, set[str]] = {}
    incoming: dict[int, int] = {
        int(r["page_id"]): int(r.get("_linkcount") or 0) for r in raw
    }
    ctop_talk: set[int] = set()
    afc_talk: set[int] = set()
    by_id = {int(r["page_id"]): r for r in raw}
    page_ids = list(by_id.keys())

    for i in range(0, len(page_ids), API_BATCH):
        batch_ids = page_ids[i : i + API_BATCH]
        data = _api_get(
            session,
            {
                "action": "query",
                "pageids": "|".join(str(x) for x in batch_ids),
                "prop": "info|categories|linkshere",
                "cllimit": "max",
                "lhnamespace": 0,
                "lhlimit": "max",
            },
        )
        for page in data.get("query", {}).get("pages", []):
            if page.get("missing"):
                continue
            pid = int(page.get("pageid") or 0)
            if pid not in by_id:
                continue
            row = by_id[pid]
            if page.get("title"):
                row["page_title"] = str(page["title"]).replace(" ", "_")
            if page.get("lastrevid"):
                row["revision_id"] = int(page["lastrevid"])
            row["avg_daily_views"] = 0
            page_cats: set[str] = set()
            for c in page.get("categories") or []:
                ctitle = c.get("title") or ""
                if ctitle.startswith("Category:"):
                    page_cats.add(ctitle[len("Category:") :].replace(" ", "_"))
            cats[pid] = page_cats
            links = page.get("linkshere") or []
            if links:
                incoming[pid] = max(incoming.get(pid, 0), len(links))
            if len(links) >= 500:
                incoming[pid] = max(incoming[pid], 500)

        talk_titles = [
            "Talk:" + decode_title(by_id[pid]["page_title"]).replace("_", " ")
            for pid in batch_ids
        ]
        talk_data = _api_get(
            session,
            {
                "action": "query",
                "titles": "|".join(talk_titles),
                "prop": "categories",
                "cllimit": "max",
            },
        )
        title_to_pid = {
            decode_title(by_id[pid]["page_title"]).replace("_", " "): pid
            for pid in batch_ids
        }
        for page in talk_data.get("query", {}).get("pages", []):
            if page.get("missing"):
                continue
            ttitle = page.get("title") or ""
            if not ttitle.startswith("Talk:"):
                continue
            article_title = ttitle[len("Talk:") :]
            pid = title_to_pid.get(article_title)
            if not pid:
                continue
            for c in page.get("categories") or []:
                ctitle = c.get("title") or ""
                if ctitle.startswith("Category:"):
                    ctitle = ctitle[len("Category:") :].replace(" ", "_")
                if ctitle == CAT_CTOP_TALK:
                    ctop_talk.add(pid)
                if ctitle == CAT_AFC_ACCEPTED:
                    afc_talk.add(pid)

        time.sleep(0.05)

    kept = [r for r in raw if r.get("revision_id")]
    if len(kept) < len(raw):
        log.warning(
            "Dropped %d pages without revision_id", len(raw) - len(kept)
        )
    return kept, cats, ctop_talk, afc_talk, incoming


def decode_title(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8")
    return str(value)


def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in row.items():
        if isinstance(v, (bytes, bytearray, memoryview)):
            out[k] = bytes(v).decode("utf-8")
        else:
            out[k] = v
    return out


def fetch_unreviewed(
    conn,
    limit: int | None,
    *,
    oldest: bool = False,
    exclude_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    order = "ASC" if oldest else "DESC"
    sql = f"""
        SELECT
            p.page_id,
            p.page_title,
            p.page_latest AS revision_id,
            p.page_len,
            ptrp.ptrp_created AS created
        FROM pagetriage_page ptrp
        JOIN page p ON p.page_id = ptrp.ptrp_page_id
        WHERE ptrp.ptrp_reviewed = 0
          AND ptrp.ptrp_deleted = 0
          AND p.page_namespace = 0
          AND p.page_is_redirect = 0
    """
    params: list[Any] = []
    if exclude_ids:
        ids = sorted(exclude_ids)
        # Chunk NOT IN if huge; typical merge excludes a few thousand.
        ph = ",".join(["%s"] * len(ids))
        sql += f" AND p.page_id NOT IN ({ph})"
        params.extend(ids)
    sql += f" ORDER BY ptrp.ptrp_created {order}"
    if limit:
        sql += f" LIMIT {int(limit)}"

    with conn.cursor() as cur:
        log.info(
            "Fetching unreviewed pages (%s%s)…",
            "oldest" if oldest else "newest",
            f", excluding {len(exclude_ids)}" if exclude_ids else "",
        )
        cur.execute(sql, tuple(params))
        rows = [_decode_row(r) for r in cur.fetchall()]
    log.info("Unreviewed mainspace articles: %d", len(rows))
    return rows


def fetch_still_unreviewed_ids(conn, page_ids: list[int]) -> set[int]:
    """Return the subset of *page_ids* still unreviewed mainspace non-redirects."""
    keep: set[int] = set()
    if not page_ids:
        return keep
    for chunk in _chunked(page_ids):
        placeholders = ",".join(["%s"] * len(chunk))
        sql = f"""
            SELECT ptrp.ptrp_page_id
            FROM pagetriage_page ptrp
            JOIN page p ON p.page_id = ptrp.ptrp_page_id
            WHERE ptrp.ptrp_page_id IN ({placeholders})
              AND ptrp.ptrp_reviewed = 0
              AND ptrp.ptrp_deleted = 0
              AND p.page_namespace = 0
              AND p.page_is_redirect = 0
        """
        with conn.cursor() as cur:
            cur.execute(sql, tuple(chunk))
            for row in cur.fetchall():
                keep.add(int(row["ptrp_page_id"]))
    log.info(
        "Still unreviewed: %d / %d snapshot IDs", len(keep), len(page_ids)
    )
    return keep


def fetch_pages_by_ids(conn, page_ids: list[int]) -> list[dict[str, Any]]:
    """Current page rows for IDs that are still unreviewed mainspace articles."""
    rows: list[dict[str, Any]] = []
    if not page_ids:
        return rows
    for chunk in _chunked(page_ids):
        placeholders = ",".join(["%s"] * len(chunk))
        sql = f"""
            SELECT
                p.page_id,
                p.page_title,
                p.page_latest AS revision_id,
                p.page_len,
                ptrp.ptrp_created AS created
            FROM pagetriage_page ptrp
            JOIN page p ON p.page_id = ptrp.ptrp_page_id
            WHERE p.page_id IN ({placeholders})
              AND ptrp.ptrp_reviewed = 0
              AND ptrp.ptrp_deleted = 0
              AND p.page_namespace = 0
              AND p.page_is_redirect = 0
        """
        with conn.cursor() as cur:
            cur.execute(sql, tuple(chunk))
            rows.extend(_decode_row(r) for r in cur.fetchall())
    log.info("Re-fetched %d / %d pages from replica", len(rows), len(page_ids))
    return rows


def _chunked(ids: list[int], size: int = CHUNK):
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


def fetch_article_categories(
    conn, page_ids: list[int], ctop_map: dict[str, str]
) -> dict[int, set[str]]:
    """Return page_id → set of relevant category titles (via linktarget)."""
    if not page_ids:
        return {}

    birth_cats = [
        f"{y}_births"
        for y in range(MINOR_BIRTH_YEAR_START, MINOR_BIRTH_YEAR_END + 1)
    ]
    exact = [
        CAT_NOTABILITY,
        CAT_PROMOTIONAL,
        CAT_ORPHAN,
        CAT_LIVING_PEOPLE,
        CAT_DISAMBIG,
        CAT_POV,
        *birth_cats,
        *ctop_map.keys(),
    ]
    # Dedupe while preserving order (stable for debugging)
    seen: set[str] = set()
    exact_unique: list[str] = []
    for c in exact:
        if c not in seen:
            seen.add(c)
            exact_unique.append(c)
    exact = exact_unique
    placeholders = ",".join(["%s"] * len(exact))
    sports_likes = [f"%{sub}%" for sub in SPORTS_SUBSTRINGS]
    violence_likes = [f"%{sub}%" for sub in VIOLENCE_SUBSTRINGS]
    substring_likes = sports_likes + violence_likes
    # linktarget.lt_title is varbinary; LOWER() alone does not fold case.
    # CONVERT to utf8mb4 so Massacres_* / Murders_* match "massacre" / "murder".
    # Film markers use RIGHT/LEFT so "_" is literal (LIKE would treat _ as wildcard).
    film_sql = """
                 OR RIGHT(LOWER(CONVERT(lt.lt_title USING utf8mb4)), 6) = '_films'
                 OR LEFT(LOWER(CONVERT(lt.lt_title USING utf8mb4)), 6) = 'films_'
                 OR LOWER(CONVERT(lt.lt_title USING utf8mb4)) = 'films'
    """
    substring_ph = " OR ".join(
        ["LOWER(CONVERT(lt.lt_title USING utf8mb4)) LIKE %s"]
        * len(substring_likes)
    )
    result: dict[int, set[str]] = {pid: set() for pid in page_ids}

    for chunk in _chunked(page_ids):
        id_ph = ",".join(["%s"] * len(chunk))
        # categorylinks now uses cl_target_id → linktarget (ns 14 = Category)
        sql = f"""
            SELECT cl.cl_from, lt.lt_title AS cat_title
            FROM categorylinks cl
            JOIN linktarget lt ON lt.lt_id = cl.cl_target_id
            WHERE cl.cl_from IN ({id_ph})
              AND lt.lt_namespace = 14
              AND (
                    lt.lt_title IN ({placeholders})
                 OR lt.lt_title LIKE %s
                 OR lt.lt_title LIKE %s
                 OR ({substring_ph})
                 {film_sql}
              )
        """
        params = (
            *chunk,
            *exact,
            CAT_COI_PREFIX + "%",
            CAT_AI_PREFIX + "%",
            *substring_likes,
        )
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur.fetchall():
                pid = int(row["cl_from"])
                cat = decode_title(row["cat_title"])
                if pid in result:
                    result[pid].add(cat)

    return result


def fetch_talk_pages_in_category(
    conn,
    page_ids: list[int],
    titles: dict[int, str],
    category_title: str,
) -> set[int]:
    """Return article page_ids whose talk page is in *category_title*."""
    if not page_ids:
        return set()

    flagged: set[int] = set()
    title_to_id = {titles[pid]: pid for pid in page_ids if pid in titles}

    for chunk in _chunked(page_ids):
        chunk_titles = [titles[pid] for pid in chunk if pid in titles]
        if not chunk_titles:
            continue
        id_ph = ",".join(["%s"] * len(chunk_titles))
        sql = f"""
            SELECT talk.page_title
            FROM page talk
            JOIN categorylinks cl ON cl.cl_from = talk.page_id
            JOIN linktarget lt ON lt.lt_id = cl.cl_target_id
            WHERE talk.page_namespace = 1
              AND talk.page_title IN ({id_ph})
              AND lt.lt_namespace = 14
              AND lt.lt_title = %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (*chunk_titles, category_title))
            for row in cur.fetchall():
                t = decode_title(row["page_title"])
                if t in title_to_id:
                    flagged.add(title_to_id[t])

    return flagged


def fetch_ctop_talk_pages(conn, page_ids: list[int], titles: dict[int, str]) -> set[int]:
    """Return article page_ids whose talk page is in the CTOP tracking category."""
    return fetch_talk_pages_in_category(conn, page_ids, titles, CAT_CTOP_TALK)


def fetch_afc_talk_pages(conn, page_ids: list[int], titles: dict[int, str]) -> set[int]:
    """Return article page_ids whose talk page is in Accepted AfC submissions."""
    return fetch_talk_pages_in_category(conn, page_ids, titles, CAT_AFC_ACCEPTED)


def fetch_incoming_counts(
    conn, page_ids: list[int], titles: dict[int, str]
) -> dict[int, int]:
    """Mainspace incoming link counts via pagelinks + linktarget."""
    counts: dict[int, int] = {pid: 0 for pid in page_ids}
    if not page_ids:
        return counts

    title_to_id = {titles[pid]: pid for pid in page_ids if pid in titles}

    for chunk in _chunked(list(title_to_id.keys()), size=CHUNK):
        id_ph = ",".join(["%s"] * len(chunk))
        sql = f"""
            SELECT lt.lt_title AS title, COUNT(*) AS incoming
            FROM linktarget lt
            JOIN pagelinks pl ON pl.pl_target_id = lt.lt_id
            WHERE lt.lt_namespace = 0
              AND lt.lt_title IN ({id_ph})
              AND pl.pl_from_namespace = 0
            GROUP BY lt.lt_title
        """
        with conn.cursor() as cur:
            cur.execute(sql, tuple(chunk))
            for row in cur.fetchall():
                t = decode_title(row["title"])
                if t in title_to_id:
                    counts[title_to_id[t]] = int(row["incoming"] or 0)

    return counts


def apply_flags(
    rows: list[dict[str, Any]],
    cats: dict[int, set[str]],
    ctop_talk: set[int],
    afc_talk: set[int],
    incoming: dict[int, int],
    ctop_map: dict[str, str],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        pid = int(row["page_id"])
        title = decode_title(row["page_title"]).replace("_", " ")
        title_us = decode_title(row["page_title"])
        page_cats = cats.get(pid, set())

        notability = CAT_NOTABILITY in page_cats
        promotional = CAT_PROMOTIONAL in page_cats
        orphan_category = CAT_ORPHAN in page_cats
        living = CAT_LIVING_PEOPLE in page_cats
        disambiguation = CAT_DISAMBIG in page_cats
        pov = CAT_POV in page_cats
        sports = page_is_sports(page_cats)
        violence = page_has_violence(page_cats)
        coi = any(c.startswith(CAT_COI_PREFIX) for c in page_cats)
        ai = any(c.startswith(CAT_AI_PREFIX) for c in page_cats)
        afc_accepted = pid in afc_talk

        birth_year = birth_year_from_categories(page_cats)
        is_minor = birth_year is not None

        ctop_labels: list[str] = []
        if pid in ctop_talk:
            ctop_labels.append("talk-notice")
        for cat in page_cats:
            label = ctop_map.get(cat)
            if label and label not in ctop_labels:
                ctop_labels.append(label)

        link_count = incoming.get(pid, 0)
        orphan_links = link_count == 0

        views = row.get("avg_daily_views")
        if views is None:
            views = 0
        else:
            views = int(views)

        created = row.get("created")
        if isinstance(created, (bytes, bytearray)):
            created = created.decode("utf-8")
        if created:
            created = str(created)

        enriched.append(
            {
                "page_id": pid,
                "title": title,
                "revision_id": int(row["revision_id"]),
                "page_len": int(row.get("page_len") or 0),
                "created": created,
                "wikipedia_url": (
                    f"https://{LANG}.wikipedia.org/wiki/"
                    f"{quote(title_us, safe=':_()/')}"
                ),
                "avg_daily_views": views,
                "pageviews_fetched_on": None,
                "incoming_links": link_count,
                "reference_need": None,
                "chatgpt": None,
                "notability": notability,
                "coi": coi,
                "promotional": promotional,
                "ai_generated": ai,
                "living_person": living,
                "is_minor": is_minor,
                "birth_year": birth_year,
                "ctop": bool(ctop_labels),
                "ctop_labels": ctop_labels,
                "orphan_links": orphan_links,
                "orphan_category": orphan_category,
                "disambiguation": disambiguation,
                "pov": pov,
                "sports": sports,
                "violence": violence,
                "afc_accepted": afc_accepted,
                "creator": None,
                "creator_blocked": False,
                "reviewer_creator": False,
            }
        )
    return enriched


def load_previous_snapshot(path: Path) -> dict[int, dict[str, Any]]:
    payload = load_snapshot_payload(path)
    by_id: dict[int, dict[str, Any]] = {}
    for article in payload.get("articles") or []:
        pid = article.get("page_id")
        if pid is not None:
            by_id[int(pid)] = article
    return by_id


def empty_snapshot_payload() -> dict[str, Any]:
    return {
        "generated_at": None,
        "lang": LANG,
        "count": 0,
        "sql_only": False,
        "source": "sql",
        "oldest": False,
        "merged": False,
        "last_prune_at": None,
        "last_ingest_at": None,
        "last_refresh_at": None,
        "articles": [],
    }


def load_snapshot_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_snapshot_payload()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read previous snapshot: %s", exc)
        return empty_snapshot_payload()
    if not isinstance(data, dict):
        return empty_snapshot_payload()
    data.setdefault("articles", [])
    return data


def snapshot_page_ids(articles: list[dict[str, Any]]) -> list[int]:
    ids: list[int] = []
    for art in articles:
        pid = art.get("page_id")
        if pid is not None:
            ids.append(int(pid))
    return ids


def keep_unreviewed_articles(
    articles: list[dict[str, Any]], keep_ids: set[int]
) -> list[dict[str, Any]]:
    """Keep snapshot rows whose page_id is still unreviewed."""
    return [a for a in articles if int(a.get("page_id") or 0) in keep_ids]


def merge_articles_by_id(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Replace or add articles keyed by page_id. Incoming wins on conflict."""
    by_id: dict[int, dict[str, Any]] = {}
    for art in existing:
        pid = art.get("page_id")
        if pid is not None:
            by_id[int(pid)] = art
    for art in incoming:
        pid = art.get("page_id")
        if pid is not None:
            by_id[int(pid)] = art
    return list(by_id.values())


def merge_reference_need(
    articles: list[dict[str, Any]],
    previous: dict[int, dict[str, Any]],
    *,
    sql_only: bool,
) -> None:
    """Fill reference_need from cache or Lift Wing. Mutates articles in place."""
    to_score: list[dict[str, Any]] = []
    cached = 0
    for art in articles:
        prev = previous.get(art["page_id"])
        if (
            prev
            and prev.get("revision_id") == art["revision_id"]
            and prev.get("reference_need") is not None
        ):
            art["reference_need"] = prev["reference_need"]
            cached += 1
        else:
            to_score.append(art)

    log.info(
        "Reference-need cache hits: %d; need scoring: %d",
        cached,
        len(to_score),
    )
    if sql_only or not to_score:
        return

    session = make_session()
    errors = 0
    for i, art in enumerate(to_score, 1):
        try:
            art["reference_need"] = score_reference_need(
                session, art["revision_id"], LANG
            )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            art["reference_need"] = None
            if errors <= 5:
                log.warning(
                    "Lift Wing failed for %s (rev %s): %s",
                    art["title"],
                    art["revision_id"],
                    exc,
                )
        if i % PROGRESS_EVERY == 0 or i == len(to_score):
            log.info("Lift Wing progress: %d / %d", i, len(to_score))


def fetch_wikitext_by_pageids(
    session, page_ids: list[int]
) -> dict[int, str]:
    """Return page_id → current main-slot wikitext via Action API."""
    out: dict[int, str] = {}
    if not page_ids:
        return out
    for i in range(0, len(page_ids), WIKITEXT_BATCH):
        batch = page_ids[i : i + WIKITEXT_BATCH]
        data = _api_get(
            session,
            {
                "action": "query",
                "pageids": "|".join(str(x) for x in batch),
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
            },
        )
        for page in data.get("query", {}).get("pages", []):
            if page.get("missing"):
                continue
            pid = int(page.get("pageid") or 0)
            revs = page.get("revisions") or []
            if not revs:
                out[pid] = ""
                continue
            rev = revs[0]
            slots = rev.get("slots") or {}
            main = slots.get("main") or {}
            content = main.get("content")
            if content is None:
                content = rev.get("content") or rev.get("*") or ""
            out[pid] = str(content)
        time.sleep(0.05)
    return out


def merge_chatgpt_flags(
    articles: list[dict[str, Any]],
    previous: dict[int, dict[str, Any]],
    session=None,
) -> None:
    """Set chatgpt bool from cache or wikitext scan. Mutates articles in place."""
    to_check: list[dict[str, Any]] = []
    cached = 0
    for art in articles:
        prev = previous.get(art["page_id"])
        if (
            prev
            and prev.get("revision_id") == art["revision_id"]
            and prev.get("chatgpt") is not None
        ):
            art["chatgpt"] = bool(prev["chatgpt"])
            cached += 1
        else:
            to_check.append(art)

    log.info(
        "ChatGPT cache hits: %d; need wikitext check: %d",
        cached,
        len(to_check),
    )
    if not to_check:
        return

    own_session = session is None
    if own_session:
        session = make_session()
        session.headers.pop("Content-Type", None)
        session.headers["User-Agent"] = get_user_agent()

    hits = 0
    for i in range(0, len(to_check), WIKITEXT_BATCH):
        batch = to_check[i : i + WIKITEXT_BATCH]
        texts = fetch_wikitext_by_pageids(
            session, [int(a["page_id"]) for a in batch]
        )
        for art in batch:
            flagged = wikitext_has_chatgpt(texts.get(art["page_id"], ""))
            art["chatgpt"] = flagged
            if flagged:
                hits += 1
        done = min(i + WIKITEXT_BATCH, len(to_check))
        if done % 100 < WIKITEXT_BATCH or done == len(to_check):
            log.info("ChatGPT wikitext progress: %d / %d", done, len(to_check))

    log.info("ChatGPT markers found: %d / %d checked", hits, len(to_check))


def fetch_page_creators(conn, page_ids: list[int]) -> dict[int, str | None]:
    """Return page_id → registered creator username (None for IP / missing)."""
    result: dict[int, str | None] = {pid: None for pid in page_ids}
    if not page_ids:
        return result

    for chunk in _chunked(page_ids):
        id_ph = ",".join(["%s"] * len(chunk))
        sql = f"""
            SELECT r.rev_page AS page_id, a.actor_name, a.actor_user
            FROM revision_userindex r
            JOIN actor a ON a.actor_id = r.rev_actor
            WHERE r.rev_page IN ({id_ph})
              AND r.rev_parent_id = 0
        """
        with conn.cursor() as cur:
            cur.execute(sql, tuple(chunk))
            for row in cur.fetchall():
                pid = int(row["page_id"])
                if pid not in result:
                    continue
                if row.get("actor_user") is None:
                    result[pid] = None
                    continue
                name = decode_title(row["actor_name"]).replace("_", " ")
                result[pid] = name or None
    return result


def fetch_page_creators_via_api(
    session, page_ids: list[int]
) -> dict[int, str | None]:
    """First-revision registered user via Action API (None for IP / missing)."""
    result: dict[int, str | None] = {pid: None for pid in page_ids}
    if not page_ids:
        return result

    for i in range(0, len(page_ids), API_BATCH):
        batch = page_ids[i : i + API_BATCH]
        data = _api_get(
            session,
            {
                "action": "query",
                "pageids": "|".join(str(x) for x in batch),
                "prop": "revisions",
                "rvdir": "newer",
                "rvlimit": 1,
                "rvprop": "user|userid",
            },
        )
        for page in data.get("query", {}).get("pages", []):
            if page.get("missing"):
                continue
            pid = int(page.get("pageid") or 0)
            revs = page.get("revisions") or []
            if not revs:
                continue
            rev = revs[0]
            userid = rev.get("userid")
            user = rev.get("user")
            if userid and user:
                result[pid] = str(user)
            else:
                result[pid] = None
        time.sleep(0.05)
    return result


def fetch_blocked_usernames(session, usernames: list[str]) -> set[str]:
    """Return the subset of usernames that are currently blocked."""
    blocked: set[str] = set()
    if not usernames:
        return blocked

    for i in range(0, len(usernames), USERS_BATCH):
        batch = usernames[i : i + USERS_BATCH]
        data = _api_get(
            session,
            {
                "action": "query",
                "list": "users",
                "ususers": "|".join(batch),
                "usprop": "blockinfo",
            },
        )
        for user in data.get("query", {}).get("users", []):
            if user.get("missing") or user.get("invalid"):
                continue
            # Currently blocked accounts include blockid / blockexpiry.
            if user.get("blockid") is not None or user.get("blockedby"):
                name = user.get("name")
                if name:
                    blocked.add(str(name))
        time.sleep(0.05)
    return blocked


def merge_creator_blocks(
    articles: list[dict[str, Any]],
    previous: dict[int, dict[str, Any]],
    *,
    creators: dict[int, str | None] | None = None,
    session=None,
) -> None:
    """Set creator + creator_blocked. Registered accounts only; IPs stay False.

    Creator names are cached from the previous snapshot when present.
    Block status is re-checked each refresh for unique registered creators.
    """
    need_ids: list[int] = []
    for art in articles:
        prev = previous.get(art["page_id"])
        if prev is not None and "creator" in prev:
            art["creator"] = prev.get("creator")
        elif creators is not None and art["page_id"] in creators:
            art["creator"] = creators.get(art["page_id"])
        else:
            need_ids.append(int(art["page_id"]))

    if need_ids:
        if session is None:
            session = make_session()
            session.headers.pop("Content-Type", None)
            session.headers["User-Agent"] = get_user_agent()
        fetched = fetch_page_creators_via_api(session, need_ids)
        by_id = {int(a["page_id"]): a for a in articles}
        for pid, name in fetched.items():
            if pid in by_id:
                by_id[pid]["creator"] = name

    for art in articles:
        if "creator" not in art:
            art["creator"] = None

    names = sorted(
        {a["creator"] for a in articles if a.get("creator")}
    )
    log.info(
        "Registered creators: %d unique across %d articles",
        len(names),
        len(articles),
    )
    if not names:
        for art in articles:
            art["creator_blocked"] = False
        return

    if session is None:
        session = make_session()
        session.headers.pop("Content-Type", None)
        session.headers["User-Agent"] = get_user_agent()

    blocked = fetch_blocked_usernames(session, names)
    log.info("Currently blocked creators: %d / %d", len(blocked), len(names))
    for art in articles:
        c = art.get("creator")
        art["creator_blocked"] = bool(c and c in blocked)


def apply_reviewer_creator_flags(articles: list[dict[str, Any]]) -> None:
    """Mark articles whose registered creator is an active NPR or AfC reviewer."""
    reviewers = load_reviewer_usernames(ROOT)
    if not reviewers:
        log.warning(
            "No reviewer list at %s — run scripts/fetch_npp_afc_reviewers.py",
            ROOT / "data" / "npp_afc_reviewers.json",
        )
    hits = 0
    for art in articles:
        creator = art.get("creator")
        flag = bool(
            creator and str(creator).replace("_", " ").strip() in reviewers
        )
        art["reviewer_creator"] = flag
        if flag:
            hits += 1
    log.info(
        "Reviewer-created articles: %d / %d (reviewers loaded: %d)",
        hits,
        len(articles),
        len(reviewers),
    )


def fetch_avg_daily_views(session, title: str) -> int:
    """Average daily views over PAGEVIEW_LOOKBACK_DAYS via Pageviews REST API."""
    end = datetime.now(timezone.utc).date() - timedelta(days=PAGEVIEW_LAG_DAYS)
    start = end - timedelta(days=PAGEVIEW_LOOKBACK_DAYS - 1)
    start_s, end_s = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    article = quote(title.replace(" ", "_"), safe="")
    url = (
        f"{PAGEVIEWS_API}/{LANG}.wikipedia/all-access/all-agents/"
        f"{article}/daily/{start_s}/{end_s}"
    )
    resp = session.get(url, timeout=30)
    if resp.status_code == 404:
        return 0
    if resp.status_code == 429:
        retry = float(resp.headers.get("Retry-After", 1))
        time.sleep(retry)
        resp = session.get(url, timeout=30)
        if resp.status_code == 404:
            return 0
    resp.raise_for_status()
    items = resp.json().get("items") or []
    if not items:
        return 0
    total = sum(int(i.get("views") or 0) for i in items)
    return int(round(total / len(items)))


def merge_pageviews(
    articles: list[dict[str, Any]],
    previous: dict[int, dict[str, Any]],
    session=None,
) -> None:
    """Fill avg_daily_views from cache (same UTC day) or Pageviews REST API."""
    today = datetime.now(timezone.utc).date().isoformat()
    to_fetch: list[dict[str, Any]] = []
    cached = 0
    for art in articles:
        prev = previous.get(art["page_id"])
        if (
            prev
            and prev.get("pageviews_fetched_on") == today
            and prev.get("avg_daily_views") is not None
        ):
            art["avg_daily_views"] = int(prev.get("avg_daily_views") or 0)
            art["pageviews_fetched_on"] = today
            cached += 1
        else:
            to_fetch.append(art)

    log.info(
        "Pageviews cache hits: %d; need REST fetch: %d",
        cached,
        len(to_fetch),
    )
    if not to_fetch:
        return

    if session is None:
        session = make_session()
        session.headers.pop("Content-Type", None)
        session.headers["User-Agent"] = get_user_agent()

    errors = 0
    nonzero = 0
    for i, art in enumerate(to_fetch, 1):
        try:
            views = fetch_avg_daily_views(session, art["title"])
            art["avg_daily_views"] = views
            art["pageviews_fetched_on"] = today
            if views > 0:
                nonzero += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            art["avg_daily_views"] = int(art.get("avg_daily_views") or 0)
            art["pageviews_fetched_on"] = None
            if errors <= 5:
                log.warning(
                    "Pageviews failed for %s: %s", art.get("title"), exc
                )
        time.sleep(PAGEVIEWS_INTERVAL)
        if i % 100 == 0 or i == len(to_fetch):
            log.info("Pageviews progress: %d / %d", i, len(to_fetch))

    log.info(
        "Pageviews fetched: %d ok (%d nonzero), %d errors",
        len(to_fetch) - errors,
        nonzero,
        errors,
    )


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".npp_queue_", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@contextmanager
def exclusive_snapshot_lock(path: Path) -> Iterator[None]:
    """Exclusive flock so prune / ingest / weekly cannot clobber each other."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as fh:
        log.info("Waiting for snapshot lock %s", lock_path)
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            log.info("Acquired snapshot lock")
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def make_api_session():
    session = make_session()
    session.headers.pop("Content-Type", None)
    session.headers["User-Agent"] = get_user_agent()
    return session


def enrich_from_replica(
    conn, raw: list[dict[str, Any]], ctop_map: dict[str, str]
) -> tuple[list[dict[str, Any]], dict[int, str | None]]:
    """SQL flags + apply_flags for replica rows. Returns (articles, creators)."""
    if not raw:
        return [], {}
    page_ids = [int(r["page_id"]) for r in raw]
    titles = {int(r["page_id"]): decode_title(r["page_title"]) for r in raw}

    log.info("Fetching category flags…")
    cats = fetch_article_categories(conn, page_ids, ctop_map)

    log.info("Fetching CTOP talk-page tags…")
    ctop_talk = fetch_ctop_talk_pages(conn, page_ids, titles)
    log.info("CTOP talk notices: %d", len(ctop_talk))

    log.info("Fetching AfC accepted talk-page tags…")
    afc_talk = fetch_afc_talk_pages(conn, page_ids, titles)
    log.info("AfC accepted (talk): %d", len(afc_talk))

    log.info("Fetching incoming link counts…")
    incoming = fetch_incoming_counts(conn, page_ids, titles)

    log.info("Fetching page creators…")
    creators = fetch_page_creators(conn, page_ids)

    articles = apply_flags(raw, cats, ctop_talk, afc_talk, incoming, ctop_map)
    return articles, creators


def attach_incremental_fields(
    articles: list[dict[str, Any]],
    previous: dict[int, dict[str, Any]],
    *,
    sql_only: bool,
    session,
    creators: dict[int, str | None] | None = None,
    pageviews: bool = True,
) -> None:
    """Fill Lift Wing / ChatGPT / creator / pageviews. Mutates articles."""
    merge_reference_need(articles, previous, sql_only=sql_only)
    merge_chatgpt_flags(articles, previous, session=session)
    merge_creator_blocks(
        articles,
        previous,
        creators=creators,
        session=session,
    )
    if pageviews:
        merge_pageviews(articles, previous, session=session)
    apply_reviewer_creator_flags(articles)


def write_scored_snapshot(
    path: Path,
    articles: list[dict[str, Any]],
    *,
    sql_only: bool,
    source: str,
    previous_payload: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    scored = [score_article(a) for a in articles]
    scored.sort(key=lambda a: (-a["score"], (a.get("title") or "").lower()))
    now = datetime.now(timezone.utc).isoformat()
    prev = previous_payload or {}
    payload: dict[str, Any] = {
        "generated_at": now,
        "lang": LANG,
        "count": len(scored),
        "sql_only": sql_only,
        "source": source,
        "oldest": bool(prev.get("oldest")),
        "merged": bool(prev.get("merged")),
        "last_prune_at": prev.get("last_prune_at"),
        "last_ingest_at": prev.get("last_ingest_at"),
        "last_refresh_at": prev.get("last_refresh_at"),
        "articles": scored,
    }
    if extra:
        payload.update(extra)
        payload["articles"] = scored
        payload["count"] = len(scored)
        payload["generated_at"] = now
    atomic_write_json(path, payload)
    log.info("Wrote %s (%d articles)", path, len(scored))
    if scored:
        top = scored[0]
        log.info(
            "Top score: %.1f — %s — %s",
            top["score"],
            top["title"],
            ", ".join(top.get("factors") or []),
        )
    return scored


def _prune_locked(
    conn, payload: dict[str, Any]
) -> tuple[list[dict[str, Any]], int]:
    """Drop reviewed/gone articles from a loaded payload. Caller holds the lock."""
    articles = list(payload.get("articles") or [])
    ids = snapshot_page_ids(articles)
    if not ids:
        return articles, 0
    keep_ids = fetch_still_unreviewed_ids(conn, ids)
    kept = keep_unreviewed_articles(articles, keep_ids)
    dropped = len(articles) - len(kept)
    log.info("Prune: kept %d, dropped %d", len(kept), dropped)
    return kept, dropped


def run_prune() -> Path:
    snapshot = ROOT / SNAPSHOT_PATH
    t0 = time.monotonic()
    from db import get_db_connection

    conn = get_db_connection()
    dropped = 0
    try:
        with exclusive_snapshot_lock(snapshot):
            payload = load_snapshot_payload(snapshot)
            kept, dropped = _prune_locked(conn, payload)
            if not payload.get("articles") and dropped == 0:
                log.info("Snapshot empty — nothing to prune")
                return snapshot
            now = datetime.now(timezone.utc).isoformat()
            write_scored_snapshot(
                snapshot,
                kept,
                sql_only=bool(payload.get("sql_only")),
                source=str(payload.get("source") or "sql"),
                previous_payload=payload,
                extra={"last_prune_at": now},
            )
    finally:
        conn.close()
    log.info("Prune finished in %.1fs (dropped %d)", time.monotonic() - t0, dropped)
    return snapshot


def run_ingest(*, sql_only: bool, limit: int) -> Path:
    """Prune, then add the newest unreviewed pages not already in the snapshot."""
    snapshot = ROOT / SNAPSHOT_PATH
    t0 = time.monotonic()
    ctop_map = load_ctop_category_map(ROOT)
    from db import get_db_connection

    conn = get_db_connection()
    try:
        with exclusive_snapshot_lock(snapshot):
            payload = load_snapshot_payload(snapshot)
            kept, dropped = _prune_locked(conn, payload)
            keep_ids = set(snapshot_page_ids(kept))
            now = datetime.now(timezone.utc).isoformat()
            write_scored_snapshot(
                snapshot,
                kept,
                sql_only=bool(payload.get("sql_only", sql_only)),
                source="sql",
                previous_payload=payload,
                extra={"last_prune_at": now},
            )

        raw = fetch_unreviewed(
            conn,
            limit,
            oldest=False,
            exclude_ids=keep_ids or None,
        )
        new_articles, creators = enrich_from_replica(conn, raw, ctop_map)

        session = make_api_session()
        previous = {int(a["page_id"]): a for a in kept if a.get("page_id") is not None}
        attach_incremental_fields(
            new_articles,
            previous,
            sql_only=sql_only,
            session=session,
            creators=creators,
            pageviews=True,
        )

        with exclusive_snapshot_lock(snapshot):
            payload2 = load_snapshot_payload(snapshot)
            existing = list(payload2.get("articles") or [])
            new_ids = snapshot_page_ids(new_articles)
            still = fetch_still_unreviewed_ids(conn, new_ids) if new_ids else set()
            new_kept = keep_unreviewed_articles(new_articles, still)
            combined = merge_articles_by_id(existing, new_kept)
            apply_reviewer_creator_flags(combined)
            now = datetime.now(timezone.utc).isoformat()
            write_scored_snapshot(
                snapshot,
                combined,
                sql_only=sql_only,
                source="sql",
                previous_payload=payload2,
                extra={
                    "last_ingest_at": now,
                    "merged": True,
                    "oldest": False,
                    "ingest_added": len(new_kept),
                },
            )
            log.info(
                "Ingest added %d (pruned %d earlier, %.1fs)",
                len(new_kept),
                dropped,
                time.monotonic() - t0,
            )
    finally:
        conn.close()
    return snapshot


def run_refresh_existing(*, sql_only: bool, limit: int | None) -> Path:
    """Re-enrich snapshot pages that are still unreviewed (weekly)."""
    snapshot = ROOT / SNAPSHOT_PATH
    t0 = time.monotonic()
    ctop_map = load_ctop_category_map(ROOT)
    from db import get_db_connection

    conn = get_db_connection()
    try:
        with exclusive_snapshot_lock(snapshot):
            payload = load_snapshot_payload(snapshot)
            articles = list(payload.get("articles") or [])
            if not articles:
                raise SystemExit(f"No snapshot at {snapshot}")
            kept, dropped = _prune_locked(conn, payload)
            now = datetime.now(timezone.utc).isoformat()
            write_scored_snapshot(
                snapshot,
                kept,
                sql_only=bool(payload.get("sql_only", sql_only)),
                source="sql",
                previous_payload=payload,
                extra={"last_prune_at": now},
            )
            previous = {
                int(a["page_id"]): a for a in kept if a.get("page_id") is not None
            }
            to_refresh = kept[: int(limit)] if limit else kept
            kept_ids = snapshot_page_ids(to_refresh)

        raw = fetch_pages_by_ids(conn, kept_ids)
        refreshed, creators = enrich_from_replica(conn, raw, ctop_map)

        session = make_api_session()
        attach_incremental_fields(
            refreshed,
            previous,
            sql_only=sql_only,
            session=session,
            creators=creators,
            pageviews=True,
        )

        with exclusive_snapshot_lock(snapshot):
            payload2 = load_snapshot_payload(snapshot)
            existing = list(payload2.get("articles") or [])
            current_ids = set(snapshot_page_ids(existing))
            incoming = [
                a
                for a in refreshed
                if int(a.get("page_id") or 0) in current_ids
            ]
            combined = merge_articles_by_id(existing, incoming)
            apply_reviewer_creator_flags(combined)
            now = datetime.now(timezone.utc).isoformat()
            write_scored_snapshot(
                snapshot,
                combined,
                sql_only=sql_only,
                source="sql",
                previous_payload=payload2,
                extra={"last_refresh_at": now},
            )
            log.info(
                "Refresh-existing updated %d (pruned %d earlier, %.1fs)",
                len(incoming),
                dropped,
                time.monotonic() - t0,
            )
    finally:
        conn.close()
    return snapshot


def run(
    *,
    sql_only: bool,
    limit: int | None,
    use_api: bool = False,
    oldest: bool = False,
    merge: bool = False,
) -> Path:
    snapshot = ROOT / SNAPSHOT_PATH
    t0 = time.monotonic()
    ctop_map = load_ctop_category_map(ROOT)
    log.info("CTOP article-side categories loaded: %d", len(ctop_map))

    previous = load_previous_snapshot(snapshot)
    exclude_ids: set[int] = set(previous.keys()) if merge else set()
    if merge:
        log.info(
            "Merge mode: keeping %d existing snapshot articles",
            len(previous),
        )
        if not limit:
            raise SystemExit("--merge requires --limit (e.g. --limit 500)")

    if use_api:
        if not limit:
            raise SystemExit("--api requires --limit (e.g. --limit 50)")
        if oldest or merge:
            raise SystemExit("--oldest/--merge are SQL-only (not supported with --api)")
        session = make_api_session()
        log.info("Fetching unreviewed via PageTriage API (limit=%d)…", limit)
        raw = fetch_unreviewed_via_api(session, limit)
        log.info("Enriching %d pages via Action API…", len(raw))
        raw, cats, ctop_talk, afc_talk, incoming = enrich_via_api(session, raw)
        log.info("CTOP talk notices: %d; AfC talk: %d", len(ctop_talk), len(afc_talk))
        articles = apply_flags(raw, cats, ctop_talk, afc_talk, incoming, ctop_map)
        creators = None
    else:
        from db import get_db_connection

        session = None
        conn = get_db_connection()
        try:
            raw = fetch_unreviewed(
                conn,
                limit,
                oldest=oldest,
                exclude_ids=exclude_ids or None,
            )
            articles, creators = enrich_from_replica(conn, raw, ctop_map)
        finally:
            conn.close()

    merge_reference_need(articles, previous, sql_only=sql_only)

    shared_session = session
    if shared_session is None:
        shared_session = make_api_session()

    merge_chatgpt_flags(articles, previous, session=shared_session)
    merge_creator_blocks(
        articles,
        previous,
        creators=creators,
        session=shared_session,
    )

    if merge and previous:
        by_id = {int(pid): dict(art) for pid, art in previous.items()}
        for art in articles:
            by_id[int(art["page_id"])] = art
        combined = list(by_id.values())
        log.info(
            "Merged %d new into snapshot (%d → %d)",
            len(articles),
            len(previous),
            len(combined),
        )
    else:
        combined = articles

    apply_reviewer_creator_flags(combined)
    merge_pageviews(combined, previous, session=shared_session)

    existing_payload = load_snapshot_payload(snapshot)
    with exclusive_snapshot_lock(snapshot):
        write_scored_snapshot(
            snapshot,
            combined,
            sql_only=sql_only,
            source="api" if use_api else "sql",
            previous_payload=existing_payload,
            extra={"oldest": oldest, "merged": merge},
        )
    elapsed = time.monotonic() - t0
    log.info("Full refresh finished in %.1fs", elapsed)
    return snapshot


def refresh_pageviews_only() -> Path:
    """Re-fetch pageviews for the existing snapshot (day-cached) and rescore."""
    snapshot = ROOT / SNAPSHOT_PATH
    if not snapshot.is_file():
        raise SystemExit(f"No snapshot at {snapshot}")
    t0 = time.monotonic()
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    articles = list(data.get("articles") or [])
    previous = {int(a["page_id"]): a for a in articles if a.get("page_id") is not None}
    session = make_api_session()
    merge_pageviews(articles, previous, session=session)
    with exclusive_snapshot_lock(snapshot):
        write_scored_snapshot(
            snapshot,
            articles,
            sql_only=bool(data.get("sql_only")),
            source=str(data.get("source") or "sql"),
            previous_payload=data,
            extra={"pageviews_refreshed": True},
        )
    log.info(
        "Pageviews refresh finished (%d articles, %.1fs)",
        len(articles),
        time.monotonic() - t0,
    )
    nonzero = sum(1 for a in articles if (a.get("avg_daily_views") or 0) > 0)
    log.info("Articles with views > 0: %d / %d", nonzero, len(articles))
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=MODES,
        default=None,
        help=(
            "Scheduled cadence: prune (hourly), ingest (daily), "
            "refresh-existing (weekly). SQL replica required."
        ),
    )
    parser.add_argument(
        "--sql-only",
        action="store_true",
        help="Skip Lift Wing; reuse cached reference_need when possible",
    )
    parser.add_argument(
        "--api",
        action="store_true",
        help="Use public Action/PageTriage APIs instead of the SQL replica",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process N unreviewed pages (ingest default: 2000)",
    )
    parser.add_argument(
        "--oldest",
        action="store_true",
        help="Take the oldest unreviewed pages (ptrp_created ASC) instead of newest",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Add fetched pages into the existing snapshot instead of replacing it",
    )
    parser.add_argument(
        "--refresh-pageviews",
        action="store_true",
        help="Only refresh Pageviews REST averages on the existing snapshot (day-cached)",
    )
    args = parser.parse_args()
    if args.mode:
        if args.api or args.merge or args.oldest or args.refresh_pageviews:
            raise SystemExit(
                "--mode cannot be combined with --api / --merge / --oldest / "
                "--refresh-pageviews"
            )
        if args.mode == "prune":
            run_prune()
            return
        if args.mode == "ingest":
            run_ingest(sql_only=args.sql_only, limit=args.limit or INGEST_LIMIT)
            return
        run_refresh_existing(sql_only=args.sql_only, limit=args.limit)
        return
    if args.refresh_pageviews:
        refresh_pageviews_only()
        return
    run(
        sql_only=args.sql_only,
        limit=args.limit,
        use_api=args.api,
        oldest=args.oldest,
        merge=args.merge,
    )


if __name__ == "__main__":
    main()
