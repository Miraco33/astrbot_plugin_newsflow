import inspect
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_newsflow.core.config.config import Settings, settings
from astrbot_plugin_newsflow.core.filter.filter import AIFilter, AITranslator
from astrbot_plugin_newsflow.core.newsletter.newsletter import NewsletterGenerator
from astrbot_plugin_newsflow.standalone.scheduler.scheduler import NewsScheduler
from astrbot_plugin_newsflow.core.storage.storage import NewsStorage


class _FakeEventStorage:
    def __init__(self, recent=None):
        self.recent = recent or {}
        self.saved = []

    def get_recent_events(self, days=7):
        return self.recent

    def save_event_fingerprint(self, *args, **kwargs):
        raise AssertionError("筛选阶段不应写入事件记忆")


class _SplitRetryClient:
    provider = 'deepseek'
    batch_size = 10
    request_delay = 0

    def __init__(self):
        self.calls = []

    def chat(self, system_prompt, user_prompt, **kwargs):
        indices = [int(value) for value in re.findall(r'^\[(\d+)\] 标题:', user_prompt, re.MULTILINE)]
        self.calls.append({'indices': indices, 'max_tokens': kwargs.get('max_tokens')})
        if len(indices) > 2:
            return '{"results":{"0":'
        results = {
            str(index): {
                'keep': 1,
                'score': 8,
                'impact_score': 8,
                'novelty_score': 7,
                'category': '科技产业',
                'story_key': f'story-{index}',
                'event_key': f'event-{index}',
                'reason': '有产业影响',
                'summary': f'测试摘要{index}',
            }
            for index in indices
        }
        return json.dumps({'results': results}, ensure_ascii=False)


class FilterPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.original_filter_settings = dict(settings.filter_settings)
        settings.filter_settings = {
            **settings.filter_settings,
            "max_news": 20,
            "candidate_pool_multiplier": 3,
            "per_source_max": 4,
            "per_story_max": 2,
            "topic_quotas": {
                "政策监管": 5,
                "财经市场": 4,
                "科技产业": 8,
                "国际局势": 3,
                "其他": 0,
            },
        }

    def tearDown(self):
        settings.filter_settings = self.original_filter_settings

    def _filter(self):
        news_filter = object.__new__(AIFilter)
        news_filter.source_priorities = {}
        news_filter._load_filter_rules = lambda: {"max_news": 20}
        return news_filter

    @staticmethod
    def _item(index, topic, source, story, score):
        return {
            "id": index,
            "title": f"测试新闻 {index}",
            "summary": f"包含明确数据和行动的测试摘要 {index}",
            "source": source,
            "topic": topic,
            "story_key": story,
            "event_key": f"event-{index}",
            "relevance_score": score,
            "novelty_score": score,
            "impact_score": score,
        }

    def test_portfolio_caps_geopolitics_story_and_source(self):
        news = []
        index = 1
        for i in range(8):
            news.append(self._item(index, "国际局势", f"国际源{i % 2}", "iran-conflict", 10 - i * 0.1))
            index += 1
        for topic, count in (("政策监管", 7), ("财经市场", 6), ("科技产业", 10)):
            for i in range(count):
                news.append(self._item(index, topic, f"专业源{i % 6}", f"{topic}-{i}", 9 - i * 0.1))
                index += 1

        selected = self._filter()._categorize_by_topic(news)
        topic_counts = Counter(item["topic"] for item in selected)
        source_counts = Counter(item["source"] for item in selected)
        story_counts = Counter(item["story_key"] for item in selected)

        self.assertEqual(len(selected), 20)
        self.assertLessEqual(topic_counts["国际局势"], 3)
        self.assertLessEqual(story_counts["iran-conflict"], 2)
        self.assertLessEqual(max(source_counts.values()), 4)
        self.assertGreaterEqual(sum(topic_counts[t] for t in ("政策监管", "财经市场", "科技产业")), 17)

    def test_event_dedup_is_side_effect_free_and_keeps_better_item(self):
        news_filter = self._filter()
        news_filter.storage = _FakeEventStorage()
        lower = self._item(1, "财经市场", "来源A", "credit-market", 6)
        better = self._item(2, "财经市场", "来源B", "credit-market", 9)
        lower["event_key"] = better["event_key"] = "same-credit-event"

        result = news_filter._event_deduplicate([lower, better])

        self.assertEqual([item["id"] for item in result], [2])

    def test_event_dedup_skips_already_published_event(self):
        news_filter = self._filter()
        news_filter.storage = _FakeEventStorage({"published-event": {"source": "旧来源"}})
        item = self._item(1, "科技产业", "新来源", "industry-story", 9)
        item["event_key"] = "published-event"

        self.assertEqual(news_filter._event_deduplicate([item]), [])

    def test_candidate_pool_is_larger_than_final_brief(self):
        self.assertEqual(self._filter()._candidate_pool_size(20), 60)

    def test_malformed_filter_batch_is_split_and_recovered(self):
        news_filter = self._filter()
        news_filter.client = _SplitRetryClient()
        batch = [self._item(i, '科技产业', '测试来源', f'story-{i}', 0) for i in range(4)]

        results, unresolved = news_filter._ai_filter_batch(batch)

        self.assertEqual(len(results), 4)
        self.assertEqual(unresolved, [])
        self.assertEqual(len(news_filter.client.calls), 3)
        self.assertTrue(all(call['max_tokens'] == 4000 for call in news_filter.client.calls))

    def test_partial_filter_json_is_rejected_for_split_retry(self):
        news_filter = self._filter()
        batch = [self._item(i, '科技产业', '测试来源', f'story-{i}', 0) for i in range(2)]
        partial = json.dumps({'results': {'0': {'keep': 0, 'score': 1}}})

        self.assertIsNone(news_filter._parse_filter_result(batch, partial))

    def test_event_dedup_skips_same_normalized_link_with_new_event_key(self):
        news_filter = self._filter()
        news_filter.storage = _FakeEventStorage({
            'legacy-event-key': {
                'source': 'BBC News',
                'link': 'https://www.bbc.co.uk/news/articles/example?at_medium=RSS&at_campaign=rss',
            },
        })
        item = self._item(1, '国际局势', 'BBC News', 'iran-conflict', 8)
        item['event_key'] = 'new-ai-event-key'
        item['link'] = 'https://bbc.co.uk/news/articles/example'

        self.assertEqual(news_filter._event_deduplicate([item]), [])

    def test_storage_local_time_marker_prevents_double_timezone_conversion(self):
        storage = object.__new__(NewsStorage)
        published = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        row = {
            'id': 1,
            'title': 'Foreign market report',
            'summary': 'Market data',
            'link': 'https://example.invalid/market',
            'source': 'Foreign Source',
            'published': published,
            'collected_at': datetime.now(timezone.utc).isoformat(),
            'category': '英文',
            'content_hash': 'hash',
            'title_original': '',
            'summary_original': '',
            'translated': 0,
        }
        news = storage._row_to_dict(row)

        self.assertEqual(news['_published_timezone'], 'Asia/Shanghai')
        self.assertEqual(self._filter()._filter_by_date([news]), [])

    def test_refill_after_dedup_uses_distinct_ai_reserve(self):
        news_filter = self._filter()
        news_filter.storage = _FakeEventStorage()
        selected = [self._item(i, '科技产业', f'来源{i}', f'selected-{i}', 8) for i in range(11)]
        reserve = [self._item(100 + i, '科技产业', f'备用{i}', f'reserve-{i}', 6) for i in range(8)]
        for i, item in enumerate(selected):
            item['title'] = f'core market event unique{i}'
            item['link'] = f'https://example.invalid/core/{i}'
        reserve_titles = [
            'central bank lowers benchmark rate',
            'chipmaker opens advanced fabrication plant',
            'biotech merger receives regulatory approval',
            'automaker cuts battery production capacity',
            'cloud provider reports record enterprise revenue',
            'shipping company reroutes vessels from strait',
            'solar manufacturer secures project financing',
            'software group acquires cybersecurity startup',
        ]
        for i, item in enumerate(reserve):
            item['title'] = reserve_titles[i]
            item['link'] = f'https://example.invalid/reserve/{i}'

        result = news_filter._refill_after_dedup(selected, reserve, 20)

        self.assertGreaterEqual(len(result), 16)

    def test_keyword_filter_keeps_only_rule_matched_eligible_items(self):
        news_filter = self._filter()
        rules = {
            "include": [{"value": "金融", "priority": 2}],
            "exclude": [{"value": "娱乐", "priority": 1}],
            "max_news": 20,
        }
        news = [
            {**self._item(1, "财经市场", "来源A", "finance", 0), "title": "金融机构公布资本补充计划"},
            {**self._item(2, "财经市场", "来源B", "entertainment", 0), "title": "娱乐公司讨论金融题材综艺"},
            {**self._item(3, "科技产业", "来源C", "unmatched", 0), "title": "制造企业扩大先进产能"},
        ]

        result = news_filter._keyword_filter(news, rules)

        self.assertEqual([item["id"] for item in result], [1])

    def test_valid_ai_rejections_are_not_refilled_from_coarse_pool(self):
        news_filter = self._filter()
        rejected = self._item(1, "财经市场", "来源A", "rejected", 2)
        rejected["_ai_assessed"] = True
        selected, reserve = news_filter._finalize_ai_assessment(
            [rejected],
            selected=[],
            unresolved=[],
            rules={"include": [], "exclude": [], "max_news": 20},
        )

        self.assertEqual(selected, [])
        self.assertEqual(reserve, [])

    def test_unresolved_ai_items_use_keyword_rules_without_unconditional_fill(self):
        news_filter = self._filter()
        matched = {**self._item(1, "财经市场", "来源A", "matched", 0), "title": "金融市场推出新工具"}
        unmatched = {**self._item(2, "科技产业", "来源B", "unmatched", 0), "title": "制造企业扩大先进产能"}
        selected, reserve = news_filter._finalize_ai_assessment(
            [matched, unmatched],
            selected=[],
            unresolved=[matched, unmatched],
            rules={
                "include": [{"value": "金融", "priority": 2}],
                "exclude": [],
                "max_news": 20,
            },
        )

        self.assertEqual([item["id"] for item in selected], [1])
        self.assertTrue(selected[0]["_ai_fallback"])
        self.assertEqual(reserve, [])

    def test_common_finalizer_applies_source_cap_in_keyword_mode(self):
        news_filter = self._filter()
        news_filter.storage = _FakeEventStorage()
        news_filter.client = SimpleNamespace(provider="none")
        candidates = []
        titles = [
            "central bank lowers benchmark rate",
            "chipmaker opens advanced fabrication plant",
            "biotech merger receives regulatory approval",
            "automaker cuts battery production capacity",
            "cloud provider reports enterprise revenue",
            "solar manufacturer secures project financing",
            "software group acquires cybersecurity startup",
            "bank launches cross-border settlement service",
        ]
        for index, title in enumerate(titles, 1):
            item = self._item(index, "科技产业", "单一来源", f"story-{index}", 8)
            item["title"] = title
            item["link"] = f"https://example.invalid/{index}"
            candidates.append(item)

        result = news_filter._finalize_candidates(candidates, [], 20, use_ai_dedup=False)

        self.assertLessEqual(len(result), settings.filter_settings["per_source_max"] + 2)

    def test_two_round_filter_does_not_restore_unassessed_coarse_items(self):
        news_filter = self._filter()
        news_filter.storage = _FakeEventStorage()
        news_filter.client = SimpleNamespace(provider="deepseek")
        news_filter._ai_semantic_filter = lambda news, rules: ([], [])
        news = [{
            **self._item(1, "科技产业", "来源A", "coarse-only", 0),
            "title": "制造企业宣布扩大先进产能",
            "link": "https://example.invalid/coarse",
        }]

        result = news_filter._two_round_filter(
            news,
            {"include": [], "exclude": [], "max_news": 20},
        )

        self.assertEqual(result, [])

    def test_ai_dedup_restores_unrelated_same_format_news(self):
        news_filter = self._filter()

        class _DedupClient:
            def chat(self, **kwargs):
                return json.dumps({"keep_indices": [0], "removed": [1]}, ensure_ascii=False)

        news_filter.client = _DedupClient()
        first = {
            **self._item(1, "科技产业", "来源A", "company-a-profit", 8),
            "title": "力源信息：上半年净利同比预增207%-217%",
            "link": "https://example.invalid/a",
        }
        second = {
            **self._item(2, "科技产业", "来源A", "company-b-profit", 8),
            "title": "协创数据：上半年净利同比预增247%-340%",
            "link": "https://example.invalid/b",
        }

        result = news_filter._deduplicate_similar([first, second])

        self.assertEqual({item["id"] for item in result}, {1, 2})

    def test_ai_dedup_accepts_grounded_cross_source_duplicate_mapping(self):
        news_filter = self._filter()

        class _DedupClient:
            def chat(self, **kwargs):
                return json.dumps({
                    "keep_indices": [0],
                    "removed": [1],
                    "duplicates": [{"remove": 1, "keep": 0, "reason": "同一谷歌广告技术裁决"}],
                }, ensure_ascii=False)

        news_filter.client = _DedupClient()
        first = {
            **self._item(1, "政策监管", "半岛电视台", "google-antitrust", 8),
            "title": "US judge rejects bid to break up Google's ad tech business",
            "summary": "A US judge rejected an antitrust request to force Google to sell its advertising technology business.",
            "event_key": "judge-rejects-google-breakup",
            "link": "https://example.invalid/aljazeera",
        }
        second = {
            **self._item(2, "政策监管", "Financial Times", "google-antitrust", 8),
            "title": "Judge rejects DOJ demand for Google to sell advertising business",
            "summary": "The court rejected the US Justice Department demand that Google divest its ad technology operation.",
            "event_key": "google-advertising-divestiture-ruling",
            "link": "https://example.invalid/ft",
        }

        result = news_filter._deduplicate_similar([first, second])

        self.assertEqual([item["id"] for item in result], [1])

    def test_ai_mapped_veto_reports_compare_chinese_summaries_separately(self):
        news_filter = self._filter()
        first = {
            "id": 1,
            "title": "Russia and China veto US-backed resolution on Iran sanctions",
            "ai_summary": "俄罗斯和中国在联合国安理会否决了美国支持的决议，该决议原本授权联合国专家继续监督对伊朗的制裁。决议于周四被否决。",
            "event_key": "iran-sanctions-panel-veto",
        }
        second = {
            "id": 2,
            "title": "Moscow and Beijing block extension of UN nuclear monitoring mandate",
            "ai_summary": "俄罗斯和中国否决美国提出的延长联合国对伊朗核计划监测的决议。",
            "event_key": "un-nuclear-monitoring-extension-blocked",
        }
        response = {"keep_indices": [0], "duplicates": [{"remove": 1, "keep": 0}]}
        result = news_filter._parse_dedup_response([first, second], json.dumps(response))
        self.assertEqual([item["id"] for item in result], [1])

        # 摘要重合本身不能替代 AI 对同一具体事件的显式判定。
        result = news_filter._parse_dedup_response(
            [first, second], json.dumps({"keep_indices": [0]})
        )
        self.assertEqual([item["id"] for item in result], [1, 2])

    def test_mapped_summary_overlap_still_protects_different_companies_and_numbers(self):
        news_filter = self._filter()
        first = {
            "title": "Alpha Semiconductor: new factory approved",
            "ai_summary": "公司宣布新建芯片生产基地，计划投资20亿元，扩充先进工艺产能并新增研发岗位。",
        }
        second = {
            "title": "Beta Technologies: expansion plan announced",
            "ai_summary": first["ai_summary"],
        }
        self.assertFalse(news_filter._has_mapped_duplicate_evidence(first, second))
        first["title"] = "Chipmaker announces a new manufacturing site"
        second["title"] = "Technology group approves a capacity investment"
        second["ai_summary"] = first["ai_summary"].replace("20亿元", "50亿元")
        self.assertFalse(news_filter._has_mapped_duplicate_evidence(first, second))

    def test_filter_prompt_requires_brief_summaries_for_domestic_sources(self):
        prompt = self._filter()._build_filter_prompt([{"title": "国内产业新闻"}])
        self.assertIn("中文源也必须重新概括", prompt)
        self.assertIn("40-60字、最多80字", prompt)

    def test_coarse_filter_excludes_unsupported_analyst_opinion(self):
        news_filter = self._filter()
        rules = {"include": [], "exclude": [], "max_news": 20}
        vague_opinion = {
            **self._item(1, "财经市场", "某财经媒体", "broker-view", 0),
            "title": "券商分析师认为市场后续走势偏强",
            "summary": "该分析师预计风险偏好将改善，但未提供新的数据或公司行动。",
        }
        numeric_forecast = {
            **self._item(3, "财经市场", "某财经媒体", "broker-target", 0),
            "title": "券商分析师预计指数将上涨10%",
            "summary": "该预测仅为分析师目标位判断，没有新增市场事实。",
        }
        vague_research = {
            **self._item(4, "科技产业", "某财经媒体", "vague-study", 0),
            "title": "研究显示雇主对新兴AI职位需求下降",
            "summary": "该研究称生成式AI正在冲击劳动力市场，但没有披露样本或数字。",
        }
        quantified_research = {
            **self._item(5, "科技产业", "某财经媒体", "quantified-study", 0),
            "title": "调查显示制造业订单增长12%",
            "summary": "该调查披露制造业订单同比增长12%。",
        }
        official_signal = {
            **self._item(2, "政策监管", "权威媒体", "fed-signal", 0),
            "title": "美联储主席表示利率路径将取决于通胀数据",
            "summary": "美联储主席在政策会议后说明利率决策条件。",
        }

        result = news_filter._coarse_filter(
            [vague_opinion, numeric_forecast, vague_research, quantified_research, official_signal],
            rules,
        )

        self.assertEqual([item["id"] for item in result], [5, 2])

    def test_ai_summary_keeps_key_numbers_and_drops_speculative_tail(self):
        news_filter = self._filter()
        news = {
            **self._item(1, "科技产业", "财经杂志", "earnings", 0),
            "title": "逸仙电商公布二季度业绩",
            "summary": "逸仙电商2026年二季度营收11.4亿元，同比增长5.1%，连续七个季度增长。",
        }
        response = json.dumps({
            "results": {
                "0": {
                    "keep": 1,
                    "score": 8,
                    "impact_score": 7,
                    "novelty_score": 8,
                    "category": "科技产业",
                    "story_key": "yatsen-earnings",
                    "event_key": "yatsen-2026-q2",
                    "reason": "公司业绩",
                    "summary": "逸仙电商二季度营收保持增长，可能影响公司估值。",
                }
            }
        }, ensure_ascii=False)

        result = news_filter._parse_filter_result([news], response)

        self.assertEqual(len(result), 1)
        self.assertNotIn("可能影响", result[0]["ai_summary"])
        self.assertRegex(result[0]["ai_summary"], r"11\.4|5\.1%")

    def test_packaged_briefing_content_is_coarsely_excluded(self):
        news_filter = self._filter()
        rules = {"include": [], "exclude": [], "max_news": 20}
        packaged = {
            **self._item(1, "财经市场", "财联社", "packaged", 0),
            "title": "【财联社早知道】光纤行业需求快速增长",
        }
        ordinary = {
            **self._item(2, "科技产业", "财联社", "ordinary", 0),
            "title": "光纤龙头宣布新增产能",
        }

        result = news_filter._coarse_filter([packaged, ordinary], rules)

        self.assertEqual([item["id"] for item in result], [2])

    def test_translation_updates_existing_ai_summary(self):
        translator = object.__new__(AITranslator)
        news = {
            "title": "Company expands capacity",
            "summary": "Company expands capacity by 20 percent.",
            "ai_summary": "The company expands capacity by 20 percent.",
        }

        translator._apply_translation(news, "公司扩大产能", "公司将产能扩大20%。")

        self.assertEqual(news["title"], "公司扩大产能")
        self.assertEqual(news["ai_summary"], "公司将产能扩大20%。")
        self.assertEqual(news["ai_summary_original"], "The company expands capacity by 20 percent.")
        self.assertEqual(news["summary"], "Company expands capacity by 20 percent.")
        self.assertNotIn("summary_original", news)

    def test_translation_batch_translates_ai_summary_used_by_newsletter(self):
        translator = object.__new__(AITranslator)

        class _TranslationClient:
            def chat(self, **kwargs):
                return "公司扩大产能\n公司将产能扩大20%。"

        translator.client = _TranslationClient()
        news = [{
            "title": "Company expands capacity",
            "summary": "Company expands capacity by 20 percent.",
            "ai_summary": "The company expands capacity by 20 percent.",
        }]

        translator._translate_batch_text(news)

        self.assertEqual(news[0]["ai_summary"], "公司将产能扩大20%。")

    def test_translation_prefixes_are_removed(self):
        self.assertEqual(AITranslator._clean_translation_line("[3] 标题：芯片公司扩大产能"), "芯片公司扩大产能")
        self.assertEqual(AITranslator._clean_translation_line("Summary: 市场供应可能改善"), "市场供应可能改善")


class _FakeNewsletterStorage:
    def __init__(self):
        self.remembered = None

    def save_newsletter(self, newsletter):
        return 1

    def remember_published_events(self, news_list):
        self.remembered = list(news_list)
        return len(news_list)


class PipelineContractTests(unittest.TestCase):
    def test_rss_sources_have_collection_caps(self):
        sources = Settings.model_fields["news_sources"].default
        for source in sources:
            if source.get("enabled") and source.get("mode") == "rss":
                with self.subTest(source=source["name"]):
                    self.assertGreater(source.get("max_articles", 0), 0)

    def test_scheduler_does_not_deduplicate_again_after_translation(self):
        source = inspect.getsource(NewsScheduler._daily_task)
        self.assertNotIn("_event_deduplicate(filtered)", source)

    def test_newsletter_does_not_render_internal_urn_as_external_link(self):
        generator = object.__new__(NewsletterGenerator)
        news = {
            "title": "财联社电报",
            "summary": "某公司公布季度经营数据",
            "ai_summary": "某公司公布季度经营数据",
            "source": "财联社",
            "published": "2026-07-19T09:00:00+08:00",
            "category": "财经",
            "topic": "财经市场",
            "link": "urn:newsflow:cls:abc123",
        }

        html = generator._generate_html("测试简报", "2026-07-19", {"财经": [news]}, 1)

        self.assertNotIn('href="urn:newsflow:cls:', html)
        self.assertIn('<span class="brief-summary">某公司公布季度经营数据</span>', html)

    def test_newsletter_bounds_domestic_and_foreign_summaries(self):
        generator = object.__new__(NewsletterGenerator)
        fact = "美联储宣布降息25个基点，联邦基金利率目标区间调整为3.75%至4%，这是本年度首次降息。"
        long_summary = fact + "会议同时讨论了就业和通胀情况，官员介绍了各项经济指标和此前政策实施背景。" * 5
        for source in ("财经杂志", "财新网", "新华社", "Financial Times"):
            for use_ai in (True, False):
                with self.subTest(source=source, ai=use_ai):
                    news = {"source": source, "summary": long_summary}
                    if use_ai:
                        news["ai_summary"] = long_summary
                    rendered = generator._generate_html("测试", "2026-09-18", {"财经": [news]}, 1)
                    summary = re.search(r'class="brief-summary">(.*?)</span>', rendered)[1]
                    self.assertLessEqual(len(summary), 100)
                    self.assertTrue(summary.endswith("。"))
                    self.assertIn("25个基点", summary)
                    self.assertIn("3.75%至4%", summary)
                    self.assertEqual(news["summary"], long_summary)

    def test_newsletter_summary_handles_short_text_and_unpunctuated_fallback(self):
        formatter = NewsletterGenerator._format_summary
        self.assertEqual(formatter("公司将产能扩大20%。"), "公司将产能扩大20%。")
        self.assertEqual(formatter(""), "")
        self.assertEqual(len(formatter("长" * 200)), 100)
        self.assertTrue(formatter("长" * 200).endswith("…"))
        self.assertNotIn("...", formatter("新闻"))
        english = "The central bank cuts rates to 3.75% and announces new measures " * 4
        self.assertIn("3.75%", formatter(english))
        self.assertLessEqual(len(formatter(english)), 100)

    def test_newsletter_persists_only_published_event_memory(self):
        storage = _FakeNewsletterStorage()
        generator = object.__new__(NewsletterGenerator)
        generator.storage = storage
        news = [{
            "title": "产业测试",
            "summary": "某公司宣布扩大产能",
            "ai_summary": "某公司宣布扩大产能，可能改善供应",
            "source": "测试来源",
            "published": "2026-07-16T06:00:00+08:00",
            "category": "财经",
            "topic": "科技产业",
            "event_key": "capacity-expansion",
            "link": "https://example.invalid/item",
        }]
        with tempfile.TemporaryDirectory() as tmp:
            generator.output_dir = tmp
            path = generator.generate(news, date_str="2026-07-16")
            self.assertTrue(Path(path).exists())
            self.assertIn("科技产业", Path(path).read_text(encoding="utf-8"))
        self.assertEqual(storage.remembered, news)


if __name__ == "__main__":
    unittest.main()
