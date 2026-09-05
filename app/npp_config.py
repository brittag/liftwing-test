"""Tunable weights and category maps for NPP review-urgency scoring."""

from __future__ import annotations

import json
from pathlib import Path

# Birth years treated as "likely under 18" (inclusive). Update annually.
MINOR_BIRTH_YEAR_START = 2008
MINOR_BIRTH_YEAR_END = 2024

# Exact category titles as stored in categorylinks / linktarget (underscores).
CAT_NOTABILITY = "All_articles_with_topics_of_unclear_notability"
CAT_PROMOTIONAL = "All_articles_with_a_promotional_tone"
CAT_ORPHAN = "All_orphaned_articles"
CAT_LIVING_PEOPLE = "Living_people"
CAT_CTOP_TALK = "Wikipedia_pages_about_contentious_topics"
CAT_DISAMBIG = "Disambiguation_pages"
CAT_POV = "All_Wikipedia_neutral_point_of_view_disputes"
# Comment documenting talk-page AfC marker (not article categories).
CAT_AFC_ACCEPTED = "Accepted_AfC_submissions"  # on Talk:, not article

# Prefix / LIKE patterns (dated subcategories).
CAT_COI_PREFIX = "Wikipedia_articles_with_possible_conflicts_of_interest_from_"
CAT_AI_PREFIX = "Articles_containing_suspected_AI-generated_texts"

# Category title substrings (case-insensitive) that mark sports-related articles.
SPORTS_SUBSTRINGS: tuple[str, ...] = (
    "sportspeople",
    "sportsmen",
    "sports",
    "players",
    "tennis",
    "football",
    "soccer",
    "handball",
    "sailing",
)

# Category title substrings (case-insensitive) for violence-related topics.
VIOLENCE_SUBSTRINGS: tuple[str, ...] = (
    "murder",
    "massacre",
    "slavery",
    "abuse",
    "trafficking",
    "terrorist",
    "sexual_assault",
    "sexual_violence",
    "political_violence",
    "war_crimes",
)

# Roots whose *subcategory trees* are expanded into data/ctop_category_closure.json.
# Kept intentionally narrow (conflict-specific). Depth/size caps live in the expand script.
CTOP_EXPAND_ROOTS: dict[str, str] = {
    "Arab–Israeli_conflict": "Arab–Israeli conflict",
    "The_Troubles_(Northern_Ireland)": "The Troubles",
    "Kurds": "Kurds and Kurdistan",
    "Kurdistan": "Kurds and Kurdistan",
    "Armenia–Azerbaijan_relations": "Armenia–Azerbaijan",
    "Nagorno-Karabakh_conflict": "Armenia–Azerbaijan",
}

# Broad topic cats: exact membership only (no subcategory walk).
CTOP_EXACT_ONLY: dict[str, str] = {
    "COVID-19": "COVID-19",
    "Abortion": "Abortion",
    "Pseudoscience": "Pseudoscience",
    "Fringe_science": "Pseudoscience",
    "Complementary_and_alternative_medicine": "CAM",
    "Genetically_modified_organisms": "GMO",
}

# Union of expand roots + exact-only (backward-compatible name for callers).
CTOP_ARTICLE_CATEGORIES: dict[str, str] = {
    **CTOP_EXPAND_ROOTS,
    **CTOP_EXACT_ONLY,
}

# Precomputed subcategory closure (run scripts/expand_ctop_categories.py).
CTOP_CLOSURE_PATH = "data/ctop_category_closure.json"

# Wikitext markers for ChatGPT-sourced citation URLs (case-insensitive substring).
CHATGPT_MARKERS: tuple[str, ...] = (
    "utm_source=chatgpt.com",
)

# Score weights (additive). Reference need and pageviews are continuous 0–max.
WEIGHTS: dict[str, float] = {
    "reference_need_max": 40.0,
    "pageviews_max": 40.0,
    "ai_generated": 20.0,
    "chatgpt": 20.0,
    "minor_blp": 20.0,
    "living_person": 10.0,
    "ctop": 40.0,
    "notability": 10.0,
    "coi": 10.0,
    "promotional": 10.0,
    "orphan_links": 10.0,
    "orphan_category_only": 5.0,
    "disambiguation": -20.0,
    "creator_blocked": 10.0,
    "reviewer_creator": -10.0,
    "afc_accepted": -10.0,
    "pov": 10.0,
    "sports": -5.0,
    "violence": 20.0,
    "page_len_max": 10.0,
    "older_than_90d": 5.0,
}

# log1p(views) / log1p(this) * pageviews_max, then capped.
# High enough that viral/news spikes outrank ordinary traffic.
PAGEVIEW_LOG_CAP = 5000.0
# REST pageviews lookback (end date is UTC today minus PAGEVIEW_LAG_DAYS).
PAGEVIEW_LOOKBACK_DAYS = 7
PAGEVIEW_LAG_DAYS = 3

# log1p(page_len) / log1p(this) * page_len_max, then capped (bytes of wikitext).
PAGE_LEN_LOG_CAP = 40000.0
# Bonus if page creation (ptrp_created) is older than this many days.
STALE_DAYS = 90

SNAPSHOT_PATH = "data/npp_queue.json"
# NPR (patroller) + AfC reviewers — built by scripts/fetch_npp_afc_reviewers.py
REVIEWERS_PATH = "data/npp_afc_reviewers.json"
LANG = "en"

_REPO_ROOT = Path(__file__).resolve().parent.parent


def load_reviewer_usernames(repo_root: Path | None = None) -> set[str]:
    """Return usernames (spaces, not underscores) of active NPR and/or AfC reviewers."""
    root = repo_root or _REPO_ROOT
    path = root / REVIEWERS_PATH
    if not path.is_file():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    reviewers = data.get("reviewers") or {}
    return {str(name).replace("_", " ").strip() for name in reviewers}


def load_ctop_category_map(repo_root: Path | None = None) -> dict[str, str]:
    """Return category_title → CTOP label for article-side matching.

    Prefers the precomputed closure file (includes tight subcategory expansion).
    Falls back to root/exact config keys only if the file is missing.
    """
    root = repo_root or _REPO_ROOT
    path = root / CTOP_CLOSURE_PATH
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        mapping: dict[str, str] = {}
        for _root_title, entry in (data.get("roots") or {}).items():
            label = entry.get("label") or _root_title
            for cat in entry.get("categories") or []:
                # First root wins if a cat appears under two trees
                mapping.setdefault(str(cat), str(label))
        if mapping:
            return mapping
    return dict(CTOP_ARTICLE_CATEGORIES)
