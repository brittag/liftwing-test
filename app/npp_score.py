"""Factor extraction and additive urgency scoring for unreviewed articles."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from npp_config import (
    CHATGPT_MARKERS,
    MINOR_BIRTH_YEAR_END,
    MINOR_BIRTH_YEAR_START,
    PAGE_LEN_LOG_CAP,
    PAGEVIEW_LOG_CAP,
    SPORTS_SUBSTRINGS,
    STALE_DAYS,
    VIOLENCE_SUBSTRINGS,
    WEIGHTS,
)


def wikitext_has_chatgpt(wikitext: str | None) -> bool:
    """True if wikitext contains a known ChatGPT citation marker."""
    if not wikitext:
        return False
    lower = wikitext.lower()
    return any(marker.lower() in lower for marker in CHATGPT_MARKERS)


def _category_has_substring(category_title: str, substrings: tuple[str, ...]) -> bool:
    lower = category_title.lower().replace(" ", "_")
    return any(sub in lower for sub in substrings)


def category_is_sports(category_title: str) -> bool:
    """True if a category title looks sports-related."""
    return _category_has_substring(category_title, SPORTS_SUBSTRINGS)


def page_is_sports(categories: set[str] | list[str]) -> bool:
    return any(category_is_sports(c) for c in categories)


# Backward-compatible aliases
category_is_athlete = category_is_sports
page_is_athlete = page_is_sports


def category_is_film(category_title: str) -> bool:
    """True if a category title marks a film page (e.g. 2026_films, Films_about_…)."""
    lower = category_title.lower().replace(" ", "_")
    return (
        lower.endswith("_films")
        or lower.startswith("films_")
        or lower == "films"
    )


def page_is_film(categories: set[str] | list[str]) -> bool:
    return any(category_is_film(c) for c in categories)


def category_is_violence(category_title: str) -> bool:
    """True if a category title matches configured violence-related substrings."""
    return _category_has_substring(category_title, VIOLENCE_SUBSTRINGS)


def page_has_violence(categories: set[str] | list[str]) -> bool:
    """True if any violence category matches and the page is not a film."""
    if page_is_film(categories):
        return False
    return any(category_is_violence(c) for c in categories)


def pageview_points(avg_daily_views: float | int | None) -> float:
    """Log-scaled pageview contribution, capped at WEIGHTS['pageviews_max']."""
    views = float(avg_daily_views or 0)
    if views <= 0:
        return 0.0
    max_pts = WEIGHTS["pageviews_max"]
    raw = max_pts * math.log1p(views) / math.log1p(PAGEVIEW_LOG_CAP)
    return min(max_pts, raw)


def page_len_points(page_len: float | int | None) -> float:
    """Log-scaled page-length contribution (wikitext bytes), 0–page_len_max."""
    length = float(page_len or 0)
    if length <= 0:
        return 0.0
    max_pts = WEIGHTS["page_len_max"]
    raw = max_pts * math.log1p(length) / math.log1p(PAGE_LEN_LOG_CAP)
    return min(max_pts, raw)


def parse_created_timestamp(created: Any) -> datetime | None:
    """Parse MediaWiki / PageTriage timestamp (YYYYMMDDHHMMSS) as UTC."""
    if created is None:
        return None
    text = str(created).strip()
    if len(text) < 14 or not text[:14].isdigit():
        return None
    try:
        return datetime.strptime(text[:14], "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def is_older_than_days(created: Any, days: int = STALE_DAYS) -> bool:
    """True if created timestamp is more than *days* before now (UTC)."""
    dt = parse_created_timestamp(created)
    if dt is None:
        return False
    age = datetime.now(timezone.utc) - dt
    return age.days > days


def reference_need_points(score: float | None) -> float:
    if score is None:
        return 0.0
    return WEIGHTS["reference_need_max"] * float(score)


def birth_year_from_categories(categories: set[str]) -> int | None:
    """Return birth year if page is in YYYY_births for a configured minor year."""
    for year in range(MINOR_BIRTH_YEAR_START, MINOR_BIRTH_YEAR_END + 1):
        if f"{year}_births" in categories:
            return year
    return None


def score_article(row: dict[str, Any]) -> dict[str, Any]:
    """Compute urgency score and factor list for one article row.

    Expected keys on *row* (booleans / values set by the refresh job):
      reference_need, avg_daily_views, ai_generated, chatgpt, living_person,
      is_minor, ctop (bool), ctop_labels (list[str]), notability, coi,
      promotional, orphan_links (incoming == 0), orphan_category,
      disambiguation, creator_blocked, reviewer_creator, afc_accepted, pov,
      sports, violence, page_len, created
    """
    factors: list[str] = []
    total = 0.0

    rn = row.get("reference_need")
    rn_pts = reference_need_points(rn)
    if rn_pts > 0:
        total += rn_pts
        factors.append(f"ref-need:{rn_pts:.1f}")

    views = row.get("avg_daily_views") or 0
    pv_pts = pageview_points(views)
    if pv_pts > 0:
        total += pv_pts
        factors.append(f"views:{pv_pts:.1f}")

    len_pts = page_len_points(row.get("page_len"))
    if len_pts > 0:
        total += len_pts
        factors.append(f"len:{len_pts:.1f}")

    if is_older_than_days(row.get("created")):
        total += WEIGHTS["older_than_90d"]
        factors.append("old")

    if row.get("ai_generated"):
        total += WEIGHTS["ai_generated"]
        factors.append("ai")

    if row.get("chatgpt"):
        total += WEIGHTS["chatgpt"]
        factors.append("chatgpt")

    if row.get("is_minor"):
        total += WEIGHTS["minor_blp"]
        factors.append("minor")

    if row.get("living_person"):
        total += WEIGHTS["living_person"]
        factors.append("blp")

    if row.get("ctop"):
        total += WEIGHTS["ctop"]
        labels = row.get("ctop_labels") or []
        label = ",".join(labels) if labels else "ctop"
        factors.append(f"ctop:{label}" if labels else "ctop")

    if row.get("notability"):
        total += WEIGHTS["notability"]
        factors.append("notability")

    if row.get("coi"):
        total += WEIGHTS["coi"]
        factors.append("coi")

    if row.get("promotional"):
        total += WEIGHTS["promotional"]
        factors.append("promotional")

    if row.get("orphan_links"):
        total += WEIGHTS["orphan_links"]
        factors.append("orphan")
    elif row.get("orphan_category"):
        total += WEIGHTS["orphan_category_only"]
        factors.append("orphan-cat")

    if row.get("disambiguation"):
        total += WEIGHTS["disambiguation"]
        factors.append("disambig")

    if row.get("creator_blocked"):
        total += WEIGHTS["creator_blocked"]
        factors.append("blocked-creator")

    if row.get("reviewer_creator"):
        total += WEIGHTS["reviewer_creator"]
        factors.append("reviewer-creator")

    if row.get("afc_accepted"):
        total += WEIGHTS["afc_accepted"]
        factors.append("AfC")

    if row.get("pov"):
        total += WEIGHTS["pov"]
        factors.append("pov")

    if row.get("sports") or row.get("athlete"):
        total += WEIGHTS["sports"]
        factors.append("sports")

    if row.get("violence"):
        total += WEIGHTS["violence"]
        factors.append("violence")

    out = dict(row)
    out["score"] = round(total, 2)
    out["factors"] = factors
    return out
