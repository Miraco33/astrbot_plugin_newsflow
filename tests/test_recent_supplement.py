import asyncio
import gc
import sqlite3
from contextlib import closing
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrbot_plugin_newsflow.core.config.config import settings
from astrbot_plugin_newsflow.core.filter.filter import AIFilter
from astrbot_plugin_newsflow.core.storage.storage import NewsStorage


class RecentSupplementTests(unittest.TestCase):
    def setUp(self):
        self.previous = settings.filter_settings
        settings.filter_settings = {**self.previous, "min_news": 16, "supplement_hours": 48}

    def tearDown(self):
        settings.filter_settings = self.previous

    def item(self, index, age=8, **changes):
        return {
            "id": index, "title": f"company{index} expands factory{index}",
            "summary": "企业公布新的产能计划。", "category": "财经",
            "link": f"https://example.invalid/{index}", "source": f"source-{index % 8}",
            "published": (datetime.now(timezone.utc) - timedelta(hours=age)).isoformat(),
            "topic": "科技产业", "event_key": f"event-{index}", "story_key": f"story-{index}",
            "relevance_score": 8, "impact_score": 8, "novelty_score": 8,
            "_ai_assessed": True, "_ai_keep": True, **changes,
        }

    def news_filter(self, memory=None):
        f = object.__new__(AIFilter)
        f.source_priorities = {}
        f.client = SimpleNamespace(is_configured=lambda: True)
        f._deduplicate_similar = Mock(side_effect=lambda items: items)
        f._deduplicate_similar_async = AsyncMock(side_effect=lambda items: items)
        f.storage = SimpleNamespace(get_recent_events=lambda **kw: memory or {})
        f._load_filter_rules = lambda: {"max_news": 20, "include": [], "exclude": []}
        return f

    def test_short_day_is_filled_from_older_core_news_without_displacing_fresh_news(self):
        fresh = [self.item(i) for i in range(1, 10)]
        older = [self.item(i, 30, relevance_score=10) for i in range(20, 30)]
        f = self.news_filter()
        f._two_round_filter = Mock(side_effect=[fresh, older])
        result = f.filter_news(fresh + older)
        self.assertEqual(len(result), 16)
        self.assertEqual({n["id"] for n in result[:9]}, {n["id"] for n in fresh})
        self.assertEqual(sum(bool(n.get("_supplemented_48h")) for n in result), 7)
        f._deduplicate_similar.assert_called_once()

    def test_sufficient_primary_day_does_not_make_an_extra_ai_call(self):
        fresh = [self.item(i) for i in range(1, 17)]
        f = self.news_filter()
        f._two_round_filter = Mock(return_value=fresh)
        self.assertEqual(f.filter_news(fresh + [self.item(99, 30)]), fresh)
        f._two_round_filter.assert_called_once()

    def test_supplement_inputs_exclude_published_links_current_rejects_and_older_than_48h(self):
        current_rejected = self.item(1)
        already_published = self.item(2, 30)
        eligible = self.item(3, 30)
        stale = self.item(4, 60)
        f = self.news_filter({"published": {"link": already_published["link"]}})
        pool = f._supplement_inputs(
            [current_rejected, already_published, eligible, stale], [current_rejected], [], f._load_filter_rules()
        )
        self.assertEqual([n["id"] for n in pool], [3])

    def test_older_geopolitics_rejected_low_score_and_duplicate_events_cannot_fill(self):
        fresh = self.item(1)
        older = [
            self.item(2, 30, topic="国际局势"),
            self.item(3, 30, _ai_keep=False),
            self.item(4, 30, relevance_score=4),
            self.item(5, 30, event_key=fresh["event_key"]),
            self.item(6, 30, _hard_excluded=True),
            self.item(7, 30),
        ]
        f = self.news_filter()
        result = f._merge_supplement([fresh], older, f._load_filter_rules())
        self.assertEqual({n["id"] for n in result}, {1, 7})

    def test_supplement_still_obeys_source_and_story_caps(self):
        f = self.news_filter()
        older = [self.item(i, 30, source="one-source") for i in range(1, 20)]
        result = f._merge_supplement([], older, f._load_filter_rules())
        self.assertLessEqual(len(result), settings.filter_settings["per_source_max"] + 2)
        older = [self.item(i, 30, story_key="one-story") for i in range(1, 20)]
        result = f._merge_supplement([], older, f._load_filter_rules())
        self.assertLessEqual(len(result), settings.filter_settings["per_story_max"])

    def test_async_path_uses_the_same_two_windows(self):
        f = self.news_filter()
        fresh, older = [self.item(1)], [self.item(2, 30)]
        f._two_round_filter_async = AsyncMock(side_effect=[fresh, older])
        result = asyncio.run(f.filter_news_async(fresh + older))
        self.assertEqual({n["id"] for n in result}, {1, 2})
        self.assertEqual(f._two_round_filter_async.await_count, 2)
        f._deduplicate_similar_async.assert_awaited_once()

    def test_no_current_news_can_still_use_an_unpublished_older_core_item(self):
        f = self.news_filter()
        older = [self.item(2, 30)]
        f._two_round_filter = Mock(return_value=older)
        self.assertEqual(f.filter_news(older), older)
        f._two_round_filter.assert_called_once()

    def test_extra_assessment_pool_has_a_bounded_size(self):
        f = self.news_filter()
        older = [self.item(i, 30) for i in range(100)]
        self.assertEqual(len(f._supplement_inputs(older, [], [], f._load_filter_rules())), 60)


class BriefingCandidateStorageTests(unittest.TestCase):
    def test_briefing_pool_does_not_inherit_ui_financial_category_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = object.__new__(NewsStorage)
            storage.db_path = str(Path(directory) / "news.db")
            storage._init_database()
            with closing(sqlite3.connect(storage.db_path)) as conn:
                conn.executemany(
                    "INSERT INTO news(title,link,category,source,published,collected_at,content_hash) VALUES (?,?,?,?,?,?,?)",
                    [(f"标题{i}", f"https://example.invalid/{i}", "财经", "测试来源", "2026-09-28T05:00:00+08:00", "2026-09-28T06:00:00+08:00", f"hash-{i}") for i in range(40)],
                )
                conn.commit()
            self.assertEqual(len(storage.get_news(limit=200)), 25)
            self.assertEqual(len(storage.get_briefing_candidates()), 40)
            gc.collect()
