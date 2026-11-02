import gc
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup

from astrbot_plugin_newsflow.core.collector.collector import NewsCollector
from astrbot_plugin_newsflow.core.filter.filter import AIFilter, _summary_lacks_context
from astrbot_plugin_newsflow.core.storage.storage import NewsStorage


class PublicationEvidenceTests(unittest.TestCase):
    source = {"name": "财经杂志", "category": "财经"}

    def setUp(self):
        self.collector = NewsCollector()

    def extract(self, html):
        return self.collector._extract_publication_time(
            BeautifulSoup(html, "html.parser"), self.source,
            "https://example.invalid/2026-09-18/article.html",
        )

    def test_scraped_article_uses_publisher_date_not_collection_date(self):
        homepage = '<a href="/2026-09-18/article.html">日本央行宣布政策利率调整</a>'
        article = '<title>日本央行宣布政策利率调整</title><span id="pubtime_baidu">2026-09-18 11:20:02</span>'
        article += "<article><p>" + "日本央行宣布将政策利率从1%上调至1.25%，公告说明决策依据。" * 3 + "</p></article>"
        responses = [SimpleNamespace(status_code=200, content=html.encode())
                     for html in (homepage, article)]
        source = {**self.source, "url": "https://example.invalid", "max_articles": 2}
        with patch.object(self.collector, "_request", side_effect=responses):
            news = self.collector._collect_by_scraping(source)
        self.assertEqual(len(news), 1)
        self.assertEqual(news[0]["published"], "2026-09-18T11:20:02+08:00")
        self.assertNotEqual(news[0]["published"], news[0]["collected_at"])
        self.assertTrue(news[0]["_publication_verified"])

    def test_update_metadata_and_body_event_date_do_not_count_as_publication(self):
        html = '<meta property="article:modified_time" content="2026-11-01T06:00:00+08:00">'
        html += '<meta name="weibo:article:update_at" content="01 11 2026 06:00:00">'
        html += "<article><p>会议在2026年9月18日召开。</p></article>"
        self.assertIsNone(self.extract(html))

    def test_original_date_wins_over_update_date(self):
        html = '<meta property="article:modified_time" content="2026-11-01T06:00:00+08:00">'
        html += '<meta property="article:published_time" content="2026-09-18T03:20:02Z">'
        self.assertEqual(self.extract(html).isoformat(), "2026-09-18T03:20:02+00:00")

    def test_jsonld_uses_matching_article_and_original_date(self):
        graph = {"@graph": [
            {"@type": "NewsArticle", "url": "https://example.invalid/related.html",
             "datePublished": "2026-11-01"},
            {"@type": "NewsArticle", "url": "https://example.invalid/2026-09-18/article.html",
             "datePublished": "2026-09-18T11:20:02+08:00", "dateModified": "2026-11-01"},
        ]}
        html = '<script type="application/ld+json">' + json.dumps(graph) + "</script>"
        self.assertEqual(self.extract(html).isoformat(), "2026-09-18T11:20:02+08:00")

    def test_chinese_time_label_is_parsed_in_china_timezone(self):
        parsed = self.extract('<span id="pubtime_baidu">发布时间：2026年10月9日 07:57</span>')
        self.assertEqual(parsed.isoformat(), "2026-10-09T07:57:00+08:00")

    def test_invalid_and_partial_dates_are_not_completed_from_the_clock(self):
        for value in ("", "unknown", "2026", "2026-09", "09-18", "2026-02-30"):
            with self.subTest(value=value):
                self.assertIsNone(self.collector._parse_publication_time(value, self.source))

    def test_missing_or_invalid_rss_dates_are_rejected(self):
        for value in ("", "not-a-date"):
            entry = {"title": "某公司宣布新增生产线", "link": "https://example.invalid/item", "published": value}
            self.assertIsNone(self.collector._parse_rss_entry(entry, self.source))

    def test_invalid_explicit_rss_publication_does_not_use_update_time(self):
        entry = {"title": "某公司宣布新增生产线", "link": "https://example.invalid/item",
                 "published": "bad-date", "updated_parsed": (2026, 11, 1, 6, 0, 0, 0, 0, 0)}
        self.assertIsNone(self.collector._parse_rss_entry(entry, self.source))

    def test_rss_update_time_alone_is_not_a_publication_date(self):
        entry = {"title": "某公司宣布新增生产线", "link": "https://example.invalid/item",
                 "updated": "2026-11-01T06:00:00Z", "updated_parsed": (2026, 11, 1, 6, 0, 0, 0, 0, 0)}
        self.assertIsNone(self.collector._parse_rss_entry(entry, self.source))

    def test_unknown_scraped_date_is_not_collected_as_fresh_news(self):
        homepage = SimpleNamespace(status_code=200, content='<a href="/2026/article">一条没有发布时间的旧新闻</a>'.encode())
        with patch.object(self.collector, "_request", return_value=homepage), patch.object(
            self.collector, "_fetch_article", return_value=("一条没有发布时间的旧新闻", "正文内容" * 30, None)
        ):
            result = self.collector._collect_by_scraping({**self.source, "url": "https://example.invalid"})
        self.assertEqual(result, [])

    def test_missing_dates_do_not_cause_unbounded_article_requests(self):
        links = ''.join(f'<a href="/2026/item-{i}">一条没有发布时间的旧新闻{i}</a>' for i in range(20))
        homepage = SimpleNamespace(status_code=200, content=links.encode())
        with patch.object(self.collector, "_request", return_value=homepage), patch.object(
            self.collector, "_fetch_article", return_value=("一条没有发布时间的旧新闻", "正文" * 30, None)
        ) as fetch:
            self.collector._collect_by_scraping({**self.source, "url": "https://example.invalid", "max_articles": 2})
        self.assertEqual(fetch.call_count, 4)


class LegacyDateTests(unittest.TestCase):
    def test_legacy_placeholder_is_quarantined_and_repaired_on_verified_recollection(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = object.__new__(NewsStorage)
            storage.db_path = str(Path(directory) / "news.db")
            storage._init_database()
            now = datetime.now(timezone(timedelta(hours=8))).isoformat()
            item = {"title": "某央行九月调整政策利率", "summary": "政策利率调整为1.25%。",
                    "source": "财经杂志", "category": "财经", "link": "https://example.invalid/old",
                    "published": now, "collected_at": now, "content_hash": "legacy"}
            storage.save_news([item])
            row = storage.get_briefing_candidates()[0]
            self.assertTrue(row["_published_unverified"])
            f = object.__new__(AIFilter)
            self.assertEqual(f._filter_by_date([row], hours=48), [])
            verified = {**item, "published": "2026-09-18T11:20:02+08:00", "_publication_verified": True}
            self.assertEqual(storage.save_news([verified]), (0, 1))
            with closing(sqlite3.connect(storage.db_path)) as conn:
                result = conn.execute("SELECT published,collected_at,summary FROM news").fetchone()
            self.assertEqual(result, (verified["published"], now, item["summary"]))
            self.assertFalse(storage.get_briefing_candidates()[0]["_published_unverified"])
            storage.save_news([{**verified, "published": now}])
            self.assertEqual(storage.get_briefing_candidates()[0]["published"], "2026-09-18 11:20")
            gc.collect()

    def test_date_admission_rejects_missing_invalid_stale_and_future_dates(self):
        now = datetime.now(timezone.utc)
        values = [None, "not-a-date", str(now.year), now.strftime("%Y-%m"),
                  (now - timedelta(days=60)).isoformat(), (now + timedelta(days=2)).isoformat()]
        f = object.__new__(AIFilter)
        self.assertEqual(f._filter_by_date([{"published": value} for value in values], hours=48), [])
        fresh = {"published": (now - timedelta(hours=2)).isoformat()}
        self.assertEqual(f._filter_by_date([fresh]), [fresh])


class ContentQualityTests(unittest.TestCase):
    def setUp(self):
        self.f = object.__new__(AIFilter)
        self.f.source_priorities = {}
        self.f.storage = SimpleNamespace(get_recent_events=lambda **kwargs: {})

    @staticmethod
    def attack_items():
        summaries = [
            "币安遭史上最大黑客攻击，失窃17亿至57亿美元，暂停存取并承诺承担损失。",
            "币安遭黑客攻击损失63亿美元，朝鲜被怀疑为幕后黑手。",
            "币安称已覆盖63亿美元黑客攻击中90%的用户损失，分析机构指朝鲜政府支持的黑客组织为幕后黑手。",
        ]
        titles = ["Exchange reports cyberattack", "Billions lost at crypto exchange", "Customers compensated after digital asset theft"]
        return [{"id": i, "title": title, "ai_summary": summary, "event_key": f"event-{i}",
                 "link": f"https://example.invalid/{i}", "published": "2026-11-01T05:00:00+08:00"}
                for i, (title, summary) in enumerate(zip(titles, summaries))]

    def test_same_attack_with_different_estimates_accepts_explicit_ai_mapping(self):
        items = self.attack_items()
        response = {"keep_indices": [2], "duplicates": [{"remove": 0, "keep": 2}, {"remove": 1, "keep": 2}]}
        self.assertEqual(self.f._parse_dedup_response(items, json.dumps(response)), [items[2]])

    def test_same_victim_incident_and_amount_deduplicates_even_if_ai_keeps_both(self):
        items = self.attack_items()[1:]
        result = self.f._parse_dedup_response(items, json.dumps({"keep_indices": [0, 1]}))
        self.assertEqual(len(result), 1)

    def test_same_incident_rule_also_works_when_ai_dedup_fails(self):
        items = self.attack_items()[1:]
        self.assertEqual(len(self.f._parse_dedup_response(items, None)), 1)

    def test_mapping_to_an_older_supplement_preserves_the_current_report(self):
        older, current = self.attack_items()[:2]
        older['_supplemented_48h'] = True
        response = {'keep_indices': [0], 'duplicates': [{'remove': 1, 'keep': 0}]}
        self.assertEqual(self.f._parse_dedup_response([older, current], json.dumps(response)), [current])

    def test_different_estimates_without_mapping_do_not_get_blindly_merged(self):
        items = self.attack_items()[:2]
        self.assertFalse(self.f._has_high_confidence_duplicate(items[0], [items[1]]))

    def test_different_victims_and_different_incident_dates_are_protected(self):
        a, b = self.attack_items()[1:]
        b["ai_summary"] = b["ai_summary"].replace("币安", "另一交易所")
        self.assertFalse(self.f._same_reported_incident(a, b))
        self.assertFalse(self.f._has_mapped_duplicate_evidence(a, b))
        b = self.attack_items()[2]
        a["ai_summary"] += "事件发生于9月18日。"
        b["ai_summary"] += "事件发生于10月31日。"
        self.assertFalse(self.f._same_reported_incident(a, b))
        self.assertFalse(self.f._has_mapped_duplicate_evidence(a, b))
        b["title"] = a["title"]
        self.assertEqual(len(self.f._parse_dedup_response([a, b], None)), 2)

    def test_recent_reserve_cannot_restore_a_rejected_article(self):
        rejected = {"id": 1, "title": "抽象的产业发展观察", "topic": "科技产业",
                    "_ai_assessed": True, "_ai_keep": False, "relevance_score": 8}
        self.assertEqual(self.f._build_ai_reserve([rejected], []), [])
        self.assertEqual(self.f._refill_after_dedup([], [rejected], 20), [])

    def test_reserve_compares_incident_with_current_selection(self):
        selected, reserve = self.attack_items()[1:]
        self.assertEqual(self.f._refill_after_dedup([selected], [reserve], 20), [selected])

    def test_unquantified_data_claims_are_rejected_but_measurements_survive(self):
        vague = "数据显示科技行业生产率上升而就业下降，或为首个AI影响劳动力市场的清晰证据。"
        concrete = "英国统计局公布第三季度科技行业生产率增长2%，就业人数下降1%。"
        self.assertTrue(_summary_lacks_context(vague))
        self.assertFalse(_summary_lacks_context(concrete))
        rules = {"exclude": []}
        bad = {"title": "Data show tech productivity rising as jobs fall", "summary": vague}
        good = {"title": "调查显示制造业订单增长12%", "summary": "该调查披露制造业订单同比增长12%。"}
        self.assertEqual(self.f._coarse_filter([bad, good], rules), [good])

    def test_vague_ai_output_cannot_pass_or_return_in_reserve(self):
        item = {"title": "Tech productivity data point to a shift", "summary": "Data show productivity rising as jobs fall."}
        response = {"results": {"0": {"keep": 1, "score": 9, "category": "科技产业",
                                      "summary": "数据显示科技行业生产率上升而就业下降。"}}}
        self.assertEqual(self.f._parse_filter_result([item], json.dumps(response)), [])
        self.assertTrue(item["_hard_excluded"])
        self.assertEqual(self.f._build_ai_reserve([item], []), [])

    def test_filter_prompt_provides_original_date_and_explicit_freshness_requirement(self):
        prompt = self.f._build_filter_prompt([{"title": "央行政策报道", "published": "2026-09-18T11:20:02+08:00"}])
        self.assertIn("2026-09-18T11:20:02+08:00", prompt)
        self.assertIn("没有新增事实", prompt)


if __name__ == "__main__":
    unittest.main()
