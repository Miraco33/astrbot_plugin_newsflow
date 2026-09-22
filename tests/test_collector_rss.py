import gc
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import feedparser

from astrbot_plugin_newsflow.core.collector.collector import NewsCollector
from astrbot_plugin_newsflow.core.config.config import settings
from astrbot_plugin_newsflow.core.storage.storage import NewsStorage


class CollectorRssTests(unittest.TestCase):
    @staticmethod
    def _cls_source(**overrides):
        source = next(
            item.copy() for item in settings.resolved_news_sources()
            if item["name"] == "财联社"
        )
        source.update(overrides)
        return source

    @staticmethod
    def _entry(published="Sun, 19 Jul 2026 01:00:00 GMT", summary="公司宣布新增产线，预计年产能提升百分之二十。"):
        xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>CLS</title>
<item><title><![CDATA[某公司扩大产能]]></title>
<description><![CDATA[<p>{summary}</p>]]></description>
<guid isPermaLink="false">某公司扩大产能</guid>
<pubDate>{published}</pubDate><category>公司</category><link></link></item>
</channel></rss>'''.encode("utf-8")
        return feedparser.parse(xml).entries[0]

    def test_cls_is_configured_as_regular_rss_source(self):
        source = self._cls_source()
        self.assertEqual(source["mode"], "rss")
        self.assertEqual(source["category"], "财经")
        self.assertEqual(source["proxy_mode"], "bypass")
        self.assertTrue(source["allow_missing_link"])
        self.assertFalse(source["fetch_full_content"])
        self.assertGreater(source["max_articles"], 0)

    def test_empty_link_gets_stable_internal_identity(self):
        collector = NewsCollector()
        source = self._cls_source()
        first = collector._parse_rss_entry(self._entry(), source)
        second = collector._parse_rss_entry(self._entry(), source)

        self.assertIsNotNone(first)
        self.assertEqual(first["link"], second["link"])
        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertTrue(first["link"].startswith("urn:newsflow:cls:"))
        self.assertEqual(first["published"], "2026-07-19T01:00:00+00:00")
        self.assertNotIn("<p>", first["summary"])

    def test_same_title_with_different_time_or_body_has_different_identity(self):
        collector = NewsCollector()
        source = self._cls_source()
        base = collector._parse_rss_entry(self._entry(), source)
        later = collector._parse_rss_entry(
            self._entry(published="Sun, 19 Jul 2026 02:00:00 GMT"),
            source,
        )
        changed = collector._parse_rss_entry(
            self._entry(summary="公司宣布第二条产线投产，预计新增不同规模产能。"),
            source,
        )

        self.assertNotEqual(base["link"], later["link"])
        self.assertNotEqual(base["link"], changed["link"])

    def test_internal_source_bypasses_environment_proxy(self):
        collector = NewsCollector()
        session = MagicMock()
        response = object()
        session.get.return_value = response
        with patch("astrbot_plugin_newsflow.core.collector.collector.requests.Session", return_value=session):
            actual = collector._request("http://rsshub:1200/cls/telegraph", self._cls_source(), 20)

        self.assertIs(actual, response)
        self.assertFalse(session.trust_env)
        session.get.assert_called_once()
        session.close.assert_called_once()

    def test_external_source_keeps_configured_proxy(self):
        collector = NewsCollector()
        collector.proxies = {"http": "http://proxy.invalid", "https": "http://proxy.invalid"}
        response = object()
        with patch("astrbot_plugin_newsflow.core.collector.collector.requests.get", return_value=response) as request:
            actual = collector._request("https://example.invalid/feed", {}, 20)

        self.assertIs(actual, response)
        self.assertEqual(request.call_args.kwargs["proxies"], collector.proxies)

    def test_repeated_internal_item_is_skipped_by_storage(self):
        collector = NewsCollector()
        news = collector._parse_rss_entry(self._entry(), self._cls_source())
        with tempfile.TemporaryDirectory() as tmp:
            storage = object.__new__(NewsStorage)
            storage.db_path = str(Path(tmp) / "news.db")
            storage._init_database()

            self.assertEqual(storage.save_news([news]), (1, 0))
            self.assertEqual(storage.save_news([news]), (0, 1))
            gc.collect()

    def test_cls_feed_respects_cap_and_does_not_fetch_web_content(self):
        collector = NewsCollector()
        source = self._cls_source(max_articles=1)
        xml = b'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>CLS</title>
<item><title>First market item</title><description>First complete market description with enough facts.</description><guid>A</guid><pubDate>Sun, 19 Jul 2026 01:00:00 GMT</pubDate><link></link></item>
<item><title>Second market item</title><description>Second complete market description with enough facts.</description><guid>B</guid><pubDate>Sun, 19 Jul 2026 02:00:00 GMT</pubDate><link></link></item>
</channel></rss>'''
        response = SimpleNamespace(status_code=200, content=xml)
        with patch.object(collector, "_request", return_value=response), patch.object(collector, "_fetch_full_content") as fetch:
            result = collector._collect_by_rss(source)

        self.assertEqual(len(result), 1)
        fetch.assert_not_called()

    def test_invalid_feed_is_an_observable_failure(self):
        collector = NewsCollector()
        response = SimpleNamespace(status_code=200, content=b"not valid rss")
        with patch.object(collector, "_request", return_value=response):
            with self.assertRaises(ValueError):
                collector._collect_by_rss(self._cls_source())


if __name__ == "__main__":
    unittest.main()
