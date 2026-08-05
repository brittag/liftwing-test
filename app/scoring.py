"""Reusable Wikipedia Lift Wing scoring helpers (no Flask dependency).

Fetches the latest revision for a page title, scores reference-need, and
counts Tone Check flags on article paragraphs.
"""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import quote

import requests

LIFTWING_BASE = "https://api.wikimedia.org/service/lw/inference/v1/models"
MIN_INTERVAL = 0.15
MAX_BATCH_SIZE = 100
TONE_THRESHOLD = 0.80
MAX_TONE_PARAGRAPHS = 40
EDIT_CHECK_BATCH = 20
MIN_PARAGRAPH_CHARS = 40
# Temporarily skip Reference Need Lift Wing calls (set True to re-enable).
ENABLE_REFERENCE_NEED = True
DEFAULT_USER_AGENT = (
    "ReferenceNeedPrototype/0.1 "
    "(User:Dreamyshade, brittag@gmail.com) "
    "research-prototype"
)

_last_request_at = 0.0


def get_user_agent() -> str:
    return os.environ.get("WIKIMEDIA_USER_AGENT", DEFAULT_USER_AGENT)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": get_user_agent(),
        "Content-Type": "application/json",
    })
    return session


def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def _normalize_title(title: str) -> str:
    return title.strip().replace(" ", "_")


def fetch_page_summary(
    session: requests.Session, title: str, lang: str
) -> dict[str, Any]:
    """Return REST page summary JSON for the latest revision."""
    encoded = quote(_normalize_title(title), safe=":_()/")
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{encoded}"
    _throttle()
    resp = session.get(url, timeout=15)
    if resp.status_code == 404:
        raise LookupError(f"Article not found: {title}")
    resp.raise_for_status()
    data = resp.json()
    if not data.get("revision"):
        raise LookupError(f"No revision id for: {title}")
    return data


def fetch_plaintext_extract(
    session: requests.Session, title: str, lang: str
) -> str:
    """Return plain-text article extract via the Action API."""
    url = f"https://{lang}.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "prop": "extracts",
        "explaintext": "1",
        "exsectionformat": "plain",
        "titles": title.strip(),
        "format": "json",
        "formatversion": "2",
        "redirects": "1",
    }
    _throttle()
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    pages = resp.json().get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        raise LookupError(f"Article extract not found: {title}")
    return pages[0].get("extract") or ""


def split_paragraphs(text: str) -> list[str]:
    """Split extract into paragraphs; drop empty/very short chunks."""
    paragraphs: list[str] = []
    for block in text.split("\n\n"):
        cleaned = " ".join(block.split()).strip()
        if len(cleaned) >= MIN_PARAGRAPH_CHARS:
            paragraphs.append(cleaned)
    return paragraphs[:MAX_TONE_PARAGRAPHS]


def call_liftwing(
    session: requests.Session, model: str, payload: dict[str, Any]
) -> dict[str, Any]:
    url = f"{LIFTWING_BASE}/{model}:predict"
    _throttle()
    resp = session.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()


def score_reference_need(
    session: requests.Session, rev_id: int, lang: str
) -> float | None:
    result = call_liftwing(
        session, "reference-need", {"rev_id": rev_id, "lang": lang}
    )
    score = result.get("reference_need_score")
    return float(score) if score is not None else None


def score_tone_flagged_count(
    session: requests.Session,
    title: str,
    paragraphs: list[str],
    lang: str,
) -> int:
    """Count paragraphs with Tone Check probability >= TONE_THRESHOLD."""
    if not paragraphs:
        return 0

    flagged = 0
    for i in range(0, len(paragraphs), EDIT_CHECK_BATCH):
        chunk = paragraphs[i : i + EDIT_CHECK_BATCH]
        payload = {
            "instances": [
                {
                    "lang": lang,
                    "check_type": "tone",
                    "page_title": title,
                    "original_text": "",
                    "modified_text": paragraph,
                }
                for paragraph in chunk
            ]
        }
        result = call_liftwing(session, "edit-check", payload)
        for pred in result.get("predictions") or []:
            if pred.get("status_code", 200) != 200:
                continue
            prob = pred.get("probability")
            if prob is not None and float(prob) >= TONE_THRESHOLD:
                flagged += 1
    return flagged


def score_article(
    session: requests.Session, title: str, lang: str
) -> dict[str, Any]:
    """Score one article. Always returns a flat result dict."""
    row: dict[str, Any] = {
        "title": title.strip(),
        "wikipedia_url": None,
        "revision_id": None,
        "reference_need": None,
        "tone_flagged_count": None,
        "error": None,
    }
    try:
        summary = fetch_page_summary(session, title, lang)
        display_title = summary.get("title") or title.strip()
        rev_id = int(summary["revision"])
        row["title"] = display_title
        row["revision_id"] = rev_id
        row["wikipedia_url"] = (
            f"https://{lang}.wikipedia.org/wiki/{quote(_normalize_title(display_title), safe=':_()/')}"
        )
        if ENABLE_REFERENCE_NEED:
            row["reference_need"] = score_reference_need(session, rev_id, lang)

        extract = fetch_plaintext_extract(session, display_title, lang)
        paragraphs = split_paragraphs(extract)
        row["tone_flagged_count"] = score_tone_flagged_count(
            session, display_title, paragraphs, lang
        )
    except Exception as exc:  # noqa: BLE001 — surface any failure as row error
        row["error"] = str(exc)
    return row


def score_batch(titles: list[str], lang: str = "en") -> dict[str, Any]:
    """Score a list of titles. Caps at MAX_BATCH_SIZE."""
    lang = (lang or "en").strip().lower() or "en"
    cleaned = [t.strip() for t in titles if t and t.strip()]
    if not cleaned:
        raise ValueError("No article titles provided")
    if len(cleaned) > MAX_BATCH_SIZE:
        raise ValueError(f"At most {MAX_BATCH_SIZE} articles per batch")

    session = make_session()
    results = [score_article(session, title, lang) for title in cleaned]
    return {"lang": lang, "results": results}
