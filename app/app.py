"""Projo — Articles to improve (paste-titles scorer) + Articles to review (NPP queue).

Run: python app/app.py
Open: http://localhost:8765         (Articles to improve)
      http://localhost:8765/review  (Articles to review)
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from npp_config import SNAPSHOT_PATH  # noqa: E402
from scoring import score_batch  # noqa: E402

STATIC_DIR = APP_DIR / "static"
PORT = int(os.environ.get("PORT", "8765"))
SNAPSHOT_FILE = ROOT / SNAPSHOT_PATH
PAGE_SIZE = 2000
SORT_FIELDS = {
    "score": "score",
    "title": "title",
    "created": "created",
    "views": "avg_daily_views",
    "need": "reference_need",
}

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")


def _load_snapshot() -> dict:
    if not SNAPSHOT_FILE.exists():
        return {
            "generated_at": None,
            "lang": "en",
            "count": 0,
            "articles": [],
            "error": (
                f"No snapshot at {SNAPSHOT_PATH}. "
                "Run: python scripts/refresh_npp_queue.py"
            ),
        }
    with SNAPSHOT_FILE.open(encoding="utf-8") as fh:
        return json.load(fh)


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _created_sort_value(created: object) -> float | None:
    text = str(created or "").strip()
    digits = text[:14]
    if len(digits) < 14 or not digits.isdigit():
        return None
    return float(digits)


def _sort_articles(articles: list[dict], sort: str, descending: bool) -> list[dict]:
    field = SORT_FIELDS.get(sort, "score")
    if field == "title":
        return sorted(
            articles,
            key=lambda a: (a.get("title") or "").casefold(),
            reverse=descending,
        )

    def key(article: dict) -> tuple[int, float]:
        if field == "created":
            numeric = _created_sort_value(article.get("created"))
        else:
            val = article.get(field)
            numeric = None if val is None else float(val)
        if numeric is None:
            return (1, 0.0)
        return (0, -numeric if descending else numeric)

    return sorted(articles, key=key)


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "score.html")


@app.get("/review")
def review_page():
    return send_from_directory(STATIC_DIR, "review.html")


@app.get("/score")
def score_page_redirect():
    """Keep old /score bookmarks working."""
    return redirect("/", code=301)


@app.get("/api/queue")
def api_queue():
    data = _load_snapshot()
    articles = list(data.get("articles") or [])

    q = (request.args.get("q") or "").strip().lower()
    min_views = request.args.get("min_views", type=float)
    if min_views is None:
        min_views = 0.0

    filters = {
        "minors": _truthy(request.args.get("minors")),
        "ctop": _truthy(request.args.get("ctop")),
        "ai": _truthy(request.args.get("ai")),
        "chatgpt": _truthy(request.args.get("chatgpt")),
        "blocked_creator": _truthy(request.args.get("blocked_creator")),
        "reviewer_creator": _truthy(request.args.get("reviewer_creator")),
        "notability": _truthy(request.args.get("notability")),
        "pov": _truthy(request.args.get("pov")),
        "athlete": _truthy(request.args.get("athlete")),
        "sports": _truthy(request.args.get("sports")),
        "violence": _truthy(request.args.get("violence")),
        "afc": _truthy(request.args.get("afc")),
        "coi": _truthy(request.args.get("coi")),
        "promotional": _truthy(request.args.get("promotional")),
        "orphan": _truthy(request.args.get("orphan")),
        "blp": _truthy(request.args.get("blp")),
        "has_views": _truthy(request.args.get("has_views")),
    }

    def keep(a: dict) -> bool:
        if q and q not in (a.get("title") or "").lower():
            return False
        views = a.get("avg_daily_views") or 0
        if views < min_views:
            return False
        if filters["has_views"] and views <= 0:
            return False
        if filters["minors"] and not a.get("is_minor"):
            return False
        if filters["ctop"] and not a.get("ctop"):
            return False
        if filters["ai"] and not a.get("ai_generated"):
            return False
        if filters["chatgpt"] and not a.get("chatgpt"):
            return False
        if filters["blocked_creator"] and not a.get("creator_blocked"):
            return False
        if filters["reviewer_creator"] and not a.get("reviewer_creator"):
            return False
        if filters["notability"] and not a.get("notability"):
            return False
        if filters["pov"] and not a.get("pov"):
            return False
        if filters["athlete"] and not (a.get("sports") or a.get("athlete")):
            return False
        if filters["sports"] and not (a.get("sports") or a.get("athlete")):
            return False
        if filters["violence"] and not a.get("violence"):
            return False
        if filters["afc"] and not a.get("afc_accepted"):
            return False
        if filters["coi"] and not a.get("coi"):
            return False
        if filters["promotional"] and not a.get("promotional"):
            return False
        if filters["orphan"] and not (
            a.get("orphan_links") or a.get("orphan_category")
        ):
            return False
        if filters["blp"] and not a.get("living_person"):
            return False
        return True

    filtered = [a for a in articles if keep(a)]

    sort = (request.args.get("sort") or "score").strip().lower()
    if sort not in SORT_FIELDS:
        sort = "score"
    descending = (request.args.get("order") or "desc").strip().lower() != "asc"
    filtered = _sort_articles(filtered, sort, descending)

    total = len(filtered)
    pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
    page = request.args.get("page", default=1, type=int) or 1
    page = min(max(1, page), pages)
    start = (page - 1) * PAGE_SIZE

    return jsonify(
        {
            "generated_at": data.get("generated_at"),
            "lang": data.get("lang", "en"),
            "count": total,
            "page": page,
            "per_page": PAGE_SIZE,
            "pages": pages,
            "total_in_snapshot": data.get("count", len(articles)),
            "sql_only": data.get("sql_only"),
            "error": data.get("error"),
            "articles": filtered[start : start + PAGE_SIZE],
        }
    )


@app.post("/api/score-batch")
def api_score_batch():
    body = request.get_json(silent=True) or {}
    titles = body.get("titles")
    lang = body.get("lang", "en")

    if isinstance(titles, str):
        titles = [line.strip() for line in titles.splitlines() if line.strip()]
    if not isinstance(titles, list):
        return jsonify({"error": "titles must be a list or newline-separated string"}), 400

    try:
        payload = score_batch(titles, lang=lang)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502

    return jsonify(payload)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
