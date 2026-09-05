#!/usr/bin/env python3
"""Fetch active New Page Reviewers and AfC reviewers; write a lookup JSON.

Same definitions as database-reports/Unreviewed-by-NPR-or-Afc-reviewers-database-report.txt:

- NPR → user_groups.ug_group = 'patroller' with active ug_expiry
- AfC → user page in Category:Wikipedia_Articles_for_Creation_reviewers
  (main user page only; joined to user table)

Usage:
  python scripts/fetch_npp_afc_reviewers.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

from db import decode_title, get_db_connection  # noqa: E402
from npp_config import REVIEWERS_PATH  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fetch_reviewers")

AFC_CATEGORY = "Wikipedia_Articles_for_Creation_reviewers"


def _norm_username(name: Any) -> str:
    text = decode_title(name).replace("_", " ").strip()
    return text


def fetch_patrollers(conn) -> dict[str, set[str]]:
    """Return username → {'NPR'} for active patrollers."""
    sql = """
        SELECT u.user_name
        FROM user_groups AS ug
        JOIN user AS u ON u.user_id = ug.ug_user
        WHERE ug.ug_group = 'patroller'
          AND (ug.ug_expiry IS NULL
               OR ug.ug_expiry > DATE_FORMAT(NOW(), '%Y%m%d%H%i%S'))
    """
    out: dict[str, set[str]] = {}
    with conn.cursor() as cur:
        cur.execute(sql)
        for row in cur.fetchall():
            name = _norm_username(row["user_name"])
            if name:
                out.setdefault(name, set()).add("NPR")
    return out


def fetch_afc_reviewers(conn) -> dict[str, set[str]]:
    """Return username → {'AfC'} for users whose main user page is in the AfC cat."""
    sql = """
        SELECT u.user_name
        FROM page AS up
        JOIN categorylinks AS cl ON cl.cl_from = up.page_id
        JOIN linktarget AS clt ON cl.cl_target_id = clt.lt_id
        JOIN user AS u ON u.user_name = REPLACE(up.page_title, '_', ' ')
        WHERE up.page_namespace = 2
          AND up.page_is_redirect = 0
          AND up.page_title NOT LIKE %s
          AND clt.lt_namespace = 14
          AND clt.lt_title = %s
    """
    out: dict[str, set[str]] = {}
    with conn.cursor() as cur:
        cur.execute(sql, ("%/%", AFC_CATEGORY))
        for row in cur.fetchall():
            name = _norm_username(row["user_name"])
            if name:
                out.setdefault(name, set()).add("AfC")
    return out


def main() -> int:
    log.info("Connecting to enwiki_p…")
    conn = get_db_connection()
    try:
        npr = fetch_patrollers(conn)
        log.info("Active NPR (patroller): %d", len(npr))
        afc = fetch_afc_reviewers(conn)
        log.info("AfC category reviewers: %d", len(afc))
    finally:
        conn.close()

    reviewers: dict[str, list[str]] = {}
    for name in sorted(set(npr) | set(afc), key=str.lower):
        roles: set[str] = set()
        if name in npr:
            roles |= npr[name]
        if name in afc:
            roles |= afc[name]
        reviewers[name] = sorted(roles)

    path = ROOT / REVIEWERS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "sql",
        "definitions": {
            "NPR": "user_groups.ug_group = patroller (active expiry)",
            "AfC": f"user page in Category:{AFC_CATEGORY.replace('_', ' ')}",
        },
        "count": len(reviewers),
        "npr_count": sum(1 for r in reviewers.values() if "NPR" in r),
        "afc_count": sum(1 for r in reviewers.values() if "AfC" in r),
        "reviewers": reviewers,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    log.info(
        "Wrote %s (%d reviewers; NPR=%d AfC=%d both=%d)",
        path,
        payload["count"],
        payload["npr_count"],
        payload["afc_count"],
        sum(1 for r in reviewers.values() if "NPR" in r and "AfC" in r),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
