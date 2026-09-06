#!/usr/bin/env python3
"""Unit tests for snapshot prune/merge helpers (no network / SQL)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "scripts"))

from db import _on_toolforge  # noqa: E402
from refresh_npp_queue import (  # noqa: E402
    keep_unreviewed_articles,
    merge_articles_by_id,
    snapshot_page_ids,
)


def _art(page_id: int, title: str = "") -> dict:
    return {"page_id": page_id, "title": title or f"P{page_id}"}


class TestKeepUnreviewed(unittest.TestCase):
    def test_drops_reviewed_and_missing(self):
        articles = [_art(1), _art(2), _art(3)]
        kept = keep_unreviewed_articles(articles, {1, 3})
        self.assertEqual([a["page_id"] for a in kept], [1, 3])

    def test_empty_keep_drops_all(self):
        articles = [_art(1), _art(2)]
        self.assertEqual(keep_unreviewed_articles(articles, set()), [])

    def test_skips_rows_without_page_id(self):
        articles = [{"title": "no-id"}, _art(9)]
        kept = keep_unreviewed_articles(articles, {9})
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["page_id"], 9)


class TestMergeArticlesById(unittest.TestCase):
    def test_incoming_replaces_existing(self):
        existing = [_art(1, "old"), _art(2, "keep")]
        incoming = [_art(1, "new")]
        merged = merge_articles_by_id(existing, incoming)
        by_id = {a["page_id"]: a["title"] for a in merged}
        self.assertEqual(by_id[1], "new")
        self.assertEqual(by_id[2], "keep")

    def test_incoming_adds_new_ids(self):
        existing = [_art(1)]
        incoming = [_art(2)]
        merged = merge_articles_by_id(existing, incoming)
        self.assertEqual(sorted(snapshot_page_ids(merged)), [1, 2])

    def test_does_not_readd_when_filtered_first(self):
        current = [_art(2)]
        refreshed = [_art(1, "pruned-should-stay-out"), _art(2, "updated")]
        current_ids = set(snapshot_page_ids(current))
        incoming = [a for a in refreshed if a["page_id"] in current_ids]
        merged = merge_articles_by_id(current, incoming)
        self.assertEqual(snapshot_page_ids(merged), [2])
        self.assertEqual(merged[0]["title"], "updated")


class TestSnapshotPageIds(unittest.TestCase):
    def test_ignores_none(self):
        self.assertEqual(snapshot_page_ids([{"page_id": None}, _art(5)]), [5])


class TestOnToolforge(unittest.TestCase):
    def test_local_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            for key in (
                "TOOL_REPLICA_USER",
                "TOOL_DATA_DIR",
                "TOOL_TOOLSDB_USER",
            ):
                os.environ.pop(key, None)
            self.assertFalse(_on_toolforge())

    def test_detects_replica_user(self):
        with patch.dict(os.environ, {"TOOL_REPLICA_USER": "u12345"}):
            self.assertTrue(_on_toolforge())


class TestSnapshotCache(unittest.TestCase):
    def test_reloads_when_mtime_changes(self):
        import app as flask_app

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "npp_queue.json"
            path.write_text(
                json.dumps({"count": 1, "articles": [{"page_id": 1}]}),
                encoding="utf-8",
            )
            old_file = flask_app.SNAPSHOT_FILE
            flask_app._snapshot_cache = None
            flask_app._snapshot_mtime = None
            flask_app.SNAPSHOT_FILE = path
            try:
                first = flask_app._load_snapshot()
                self.assertEqual(first["count"], 1)
                self.assertIs(flask_app._load_snapshot(), first)
                path.write_text(
                    json.dumps({"count": 0, "articles": []}),
                    encoding="utf-8",
                )
                os.utime(path, (path.stat().st_mtime + 5, path.stat().st_mtime + 5))
                second = flask_app._load_snapshot()
                self.assertEqual(second["count"], 0)
                self.assertIsNot(second, first)
            finally:
                flask_app.SNAPSHOT_FILE = old_file
                flask_app._snapshot_cache = None
                flask_app._snapshot_mtime = None


if __name__ == "__main__":
    unittest.main()
