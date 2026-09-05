#!/usr/bin/env python3
"""Unit tests for NPP urgency scoring (no network / SQL)."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from npp_config import PAGE_LEN_LOG_CAP, PAGEVIEW_LOG_CAP, STALE_DAYS, WEIGHTS  # noqa: E402
from npp_score import (  # noqa: E402
    birth_year_from_categories,
    is_older_than_days,
    page_len_points,
    pageview_points,
    reference_need_points,
    score_article,
    wikitext_has_chatgpt,
)


class TestPageviewPoints(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(pageview_points(0), 0.0)
        self.assertEqual(pageview_points(None), 0.0)

    def test_cap_at_log_cap(self):
        self.assertAlmostEqual(
            pageview_points(PAGEVIEW_LOG_CAP), WEIGHTS["pageviews_max"], places=5
        )
        self.assertAlmostEqual(
            pageview_points(PAGEVIEW_LOG_CAP * 10),
            WEIGHTS["pageviews_max"],
            places=5,
        )

    def test_mid_range(self):
        expected = (
            WEIGHTS["pageviews_max"]
            * math.log1p(10)
            / math.log1p(PAGEVIEW_LOG_CAP)
        )
        self.assertAlmostEqual(pageview_points(10), expected, places=5)


class TestScoreArticle(unittest.TestCase):
    def test_stacks_factors(self):
        row = {
            "title": "Example",
            "reference_need": 0.5,
            "avg_daily_views": 0,
            "ai_generated": True,
            "is_minor": True,
            "living_person": True,
            "ctop": True,
            "ctop_labels": ["talk-notice"],
            "notability": True,
            "coi": False,
            "promotional": False,
            "orphan_links": True,
            "orphan_category": False,
        }
        out = score_article(row)
        expected = (
            WEIGHTS["reference_need_max"] * 0.5
            + WEIGHTS["ai_generated"]
            + WEIGHTS["minor_blp"]
            + WEIGHTS["living_person"]
            + WEIGHTS["ctop"]
            + WEIGHTS["notability"]
            + WEIGHTS["orphan_links"]
        )
        self.assertAlmostEqual(out["score"], round(expected, 2), places=2)
        self.assertIn("ai", out["factors"])
        self.assertIn("minor", out["factors"])
        self.assertTrue(any(f.startswith("ctop") for f in out["factors"]))

    def test_orphan_category_weaker(self):
        a = score_article(
            {
                "reference_need": None,
                "avg_daily_views": 0,
                "orphan_links": True,
                "orphan_category": True,
            }
        )
        b = score_article(
            {
                "reference_need": None,
                "avg_daily_views": 0,
                "orphan_links": False,
                "orphan_category": True,
            }
        )
        self.assertEqual(a["score"], WEIGHTS["orphan_links"])
        self.assertEqual(b["score"], WEIGHTS["orphan_category_only"])


class TestBirthYear(unittest.TestCase):
    def test_detects_minor_year(self):
        self.assertEqual(
            birth_year_from_categories({"2015_births", "Living_people"}),
            2015,
        )

    def test_ignores_adult(self):
        self.assertIsNone(
            birth_year_from_categories({"1990_births", "Living_people"})
        )


class TestReferenceNeed(unittest.TestCase):
    def test_none_is_zero(self):
        self.assertEqual(reference_need_points(None), 0.0)

    def test_full(self):
        self.assertEqual(reference_need_points(1.0), WEIGHTS["reference_need_max"])


class TestChatGPTMarker(unittest.TestCase):
    def test_detects_utm(self):
        self.assertTrue(
            wikitext_has_chatgpt(
                "{{cite web|url=https://ex.com/a?utm_source=chatgpt.com}}"
            )
        )
        self.assertTrue(
            wikitext_has_chatgpt("…&UTM_SOURCE=ChatGPT.com&utm_medium=…")
        )

    def test_negative(self):
        self.assertFalse(wikitext_has_chatgpt("{{cite web|url=https://ex.com}}"))
        self.assertFalse(wikitext_has_chatgpt(None))
        self.assertFalse(wikitext_has_chatgpt(""))

    def test_score_factor(self):
        out = score_article(
            {
                "reference_need": None,
                "avg_daily_views": 0,
                "chatgpt": True,
            }
        )
        self.assertEqual(out["score"], WEIGHTS["chatgpt"])
        self.assertIn("chatgpt", out["factors"])


class TestDisambiguation(unittest.TestCase):
    def test_penalty(self):
        out = score_article(
            {
                "reference_need": None,
                "avg_daily_views": 0,
                "disambiguation": True,
                "orphan_links": True,
            }
        )
        self.assertEqual(
            out["score"],
            WEIGHTS["orphan_links"] + WEIGHTS["disambiguation"],
        )
        self.assertIn("disambig", out["factors"])


class TestCreatorBlocked(unittest.TestCase):
    def test_bonus(self):
        out = score_article(
            {
                "reference_need": None,
                "avg_daily_views": 0,
                "creator_blocked": True,
            }
        )
        self.assertEqual(out["score"], WEIGHTS["creator_blocked"])
        self.assertIn("blocked-creator", out["factors"])

    def test_reviewer_creator(self):
        out = score_article(
            {
                "reference_need": None,
                "avg_daily_views": 0,
                "reviewer_creator": True,
            }
        )
        self.assertEqual(out["score"], WEIGHTS["reviewer_creator"])
        self.assertIn("reviewer-creator", out["factors"])


class TestPovSportsAndAfc(unittest.TestCase):
    def test_pov(self):
        out = score_article(
            {"reference_need": None, "avg_daily_views": 0, "pov": True}
        )
        self.assertEqual(out["score"], WEIGHTS["pov"])
        self.assertIn("pov", out["factors"])

    def test_sports_penalty(self):
        out = score_article(
            {
                "reference_need": None,
                "avg_daily_views": 0,
                "living_person": True,
                "sports": True,
            }
        )
        self.assertEqual(
            out["score"],
            WEIGHTS["living_person"] + WEIGHTS["sports"],
        )
        self.assertIn("sports", out["factors"])
        # Legacy athlete flag still scores as sports.
        legacy = score_article(
            {"reference_need": None, "avg_daily_views": 0, "athlete": True}
        )
        self.assertEqual(legacy["score"], WEIGHTS["sports"])
        self.assertIn("sports", legacy["factors"])

    def test_sports_category_match(self):
        from npp_score import category_is_sports, page_is_sports

        self.assertTrue(category_is_sports("American_football_players"))
        self.assertTrue(category_is_sports("English_sportspeople"))
        self.assertTrue(category_is_sports("Finnish_sportsmen"))
        self.assertTrue(category_is_sports("2024_in_tennis"))
        self.assertTrue(category_is_sports("Association_football"))
        self.assertTrue(category_is_sports("Handball_players"))
        self.assertTrue(category_is_sports("Olympic_sailing"))
        self.assertTrue(page_is_sports({"Living_people", "Olympic_sports"}))
        self.assertFalse(category_is_sports("Living_people"))

    def test_afc_accepted(self):
        out = score_article(
            {
                "reference_need": None,
                "avg_daily_views": 0,
                "afc_accepted": True,
            }
        )
        self.assertEqual(out["score"], WEIGHTS["afc_accepted"])
        self.assertIn("AfC", out["factors"])

    def test_violence(self):
        from npp_score import category_is_film, category_is_violence, page_has_violence

        out = score_article(
            {"reference_need": None, "avg_daily_views": 0, "violence": True}
        )
        self.assertEqual(out["score"], WEIGHTS["violence"])
        self.assertIn("violence", out["factors"])
        self.assertTrue(category_is_violence("Massacres_in_Syria"))
        self.assertTrue(category_is_violence("Human_trafficking"))
        self.assertTrue(category_is_violence("Sexual_assault"))
        self.assertTrue(category_is_violence("Sexual_violence_in_India"))
        self.assertTrue(category_is_violence("Political_violence_in_Bangladesh"))
        self.assertTrue(category_is_violence("War_crimes_in_Ukraine"))
        self.assertTrue(page_has_violence({"Living_people", "Murdered_politicians"}))
        self.assertFalse(category_is_violence("Living_people"))
        # Films about violence topics must not get the violence bump.
        self.assertTrue(category_is_film("2026_films"))
        self.assertTrue(category_is_film("Films_about_mass_murder"))
        self.assertFalse(
            page_has_violence({"2026_films", "Films_about_mass_murder"})
        )
        self.assertTrue(
            category_is_violence("Films_about_mass_murder")
        )  # substring still matches; page-level excludes films


class TestPageLenAndAge(unittest.TestCase):
    def test_page_len_cap(self):
        self.assertEqual(page_len_points(0), 0.0)
        self.assertAlmostEqual(
            page_len_points(PAGE_LEN_LOG_CAP), WEIGHTS["page_len_max"], places=5
        )
        self.assertAlmostEqual(
            page_len_points(PAGE_LEN_LOG_CAP * 5),
            WEIGHTS["page_len_max"],
            places=5,
        )

    def test_page_len_mid(self):
        expected = (
            WEIGHTS["page_len_max"]
            * math.log1p(7000)
            / math.log1p(PAGE_LEN_LOG_CAP)
        )
        self.assertAlmostEqual(page_len_points(7000), expected, places=5)

    def test_older_than_90d(self):
        self.assertTrue(is_older_than_days("20200101120000", days=STALE_DAYS))
        self.assertFalse(is_older_than_days("20990101120000", days=STALE_DAYS))
        out = score_article(
            {
                "reference_need": None,
                "avg_daily_views": 0,
                "page_len": 0,
                "created": "20200101120000",
            }
        )
        self.assertEqual(out["score"], WEIGHTS["older_than_90d"])
        self.assertIn("old", out["factors"])


if __name__ == "__main__":
    unittest.main()
