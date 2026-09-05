#!/usr/bin/env python3
"""Build a tight CTOP subcategory closure for article-side matching.

Expands CTOP_EXPAND_ROOTS a few levels deep (capped), and records
CTOP_EXACT_ONLY as root-only. Writes data/ctop_category_closure.json.

Usage:
  python scripts/expand_ctop_categories.py
  python scripts/expand_ctop_categories.py --depth 2 --max-per-root 80
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

from npp_config import (  # noqa: E402
    CTOP_CLOSURE_PATH,
    CTOP_EXACT_ONLY,
    CTOP_EXPAND_ROOTS,
    LANG,
)
from scoring import get_user_agent  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("expand_ctop")

API = f"https://{LANG}.wikipedia.org/w/api.php"
DEFAULT_DEPTH = 2
DEFAULT_MAX_PER_ROOT = 80


def _api_get(session: requests.Session, params: dict[str, Any]) -> dict[str, Any]:
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


def fetch_subcategories(
    session: requests.Session, category_title: str
) -> list[str]:
    """Return direct subcategory titles (underscored, no Category: prefix)."""
    titles: list[str] = []
    cont: str | None = None
    while True:
        params: dict[str, Any] = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": f"Category:{category_title.replace('_', ' ')}",
            "cmtype": "subcat",
            "cmlimit": "max",
        }
        if cont:
            params["cmcontinue"] = cont
        data = _api_get(session, params)
        for m in data.get("query", {}).get("categorymembers") or []:
            t = m.get("title") or ""
            if t.startswith("Category:"):
                titles.append(t[len("Category:") :].replace(" ", "_"))
        cont = (data.get("continue") or {}).get("cmcontinue")
        if not cont:
            break
        time.sleep(0.05)
    return titles


def expand_root(
    session: requests.Session,
    root: str,
    *,
    depth: int,
    max_per_root: int,
) -> list[str]:
    """BFS subcategory expansion. Always includes the root itself."""
    seen: set[str] = {root}
    # queue of (title, depth_from_root)
    queue: list[tuple[str, int]] = [(root, 0)]
    ordered: list[str] = [root]

    while queue and len(ordered) < max_per_root:
        title, d = queue.pop(0)
        if d >= depth:
            continue
        try:
            children = fetch_subcategories(session, title)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed subcats for %s: %s", title, exc)
            continue
        for child in children:
            if child in seen:
                continue
            seen.add(child)
            ordered.append(child)
            if len(ordered) >= max_per_root:
                log.info(
                    "  %s: hit max_per_root=%d (depth cap %d)",
                    root,
                    max_per_root,
                    depth,
                )
                break
            queue.append((child, d + 1))
        time.sleep(0.05)

    return ordered


def build_closure(*, depth: int, max_per_root: int) -> dict[str, Any]:
    session = requests.Session()
    session.headers["User-Agent"] = get_user_agent()

    roots_out: dict[str, Any] = {}

    log.info(
        "Expanding %d tight roots (depth=%d, max_per_root=%d)…",
        len(CTOP_EXPAND_ROOTS),
        depth,
        max_per_root,
    )
    for root, label in CTOP_EXPAND_ROOTS.items():
        cats = expand_root(
            session, root, depth=depth, max_per_root=max_per_root
        )
        roots_out[root] = {
            "label": label,
            "expand": True,
            "categories": cats,
            "count": len(cats),
        }
        log.info("  %s → %d categories", root, len(cats))

    log.info("Recording %d exact-only roots (no expansion)…", len(CTOP_EXACT_ONLY))
    for root, label in CTOP_EXACT_ONLY.items():
        roots_out[root] = {
            "label": label,
            "expand": False,
            "categories": [root],
            "count": 1,
        }

    # Deduped flat size for logging
    flat = {c for e in roots_out.values() for c in e["categories"]}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lang": LANG,
        "depth": depth,
        "max_per_root": max_per_root,
        "total_unique_categories": len(flat),
        "roots": roots_out,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--depth",
        type=int,
        default=DEFAULT_DEPTH,
        help=f"Max subcategory depth for expand roots (default {DEFAULT_DEPTH})",
    )
    parser.add_argument(
        "--max-per-root",
        type=int,
        default=DEFAULT_MAX_PER_ROOT,
        help=f"Cap categories collected per expand root (default {DEFAULT_MAX_PER_ROOT})",
    )
    args = parser.parse_args()

    payload = build_closure(depth=args.depth, max_per_root=args.max_per_root)
    out = ROOT / CTOP_CLOSURE_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log.info(
        "Wrote %s (%d unique categories across %d roots)",
        out,
        payload["total_unique_categories"],
        len(payload["roots"]),
    )


if __name__ == "__main__":
    main()
