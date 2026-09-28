import unittest

from astrbot_plugin_newsflow.core.filter.filter import (
    AITranslator,
    _ensure_summary_context,
    _summary_lacks_context,
)


class SummaryContextTests(unittest.TestCase):
    def test_reaction_without_an_event_uses_the_existing_chinese_title(self):
        title = "美国宣布延长部分进口商品关税豁免期限"
        self.assertEqual(
            _ensure_summary_context("美国总统特朗普立即作出反应。", title), title
        )

    def test_translation_repairs_existing_chinese_teaser_instead_of_preserving_it(self):
        news = {
            "title": "US announces an extension of tariff exemptions",
            "summary": "The government extended exemptions for selected imports.",
            "ai_summary": "美国总统特朗普立即作出反应。",
        }
        title = "美国宣布延长部分进口商品关税豁免期限"
        object.__new__(AITranslator)._apply_translation(
            news, title, "美国宣布延长部分进口商品关税豁免期限。"
        )
        self.assertEqual(news["ai_summary"], title)
        self.assertEqual(news["summary"], "The government extended exemptions for selected imports.")

    def test_a_short_concrete_fact_is_not_rejected_for_being_short(self):
        for summary in ("央行降息25个基点。", "法院撤销该禁令。", "特朗普宣布延长停火48小时作为回应。"):
            with self.subTest(summary=summary):
                self.assertFalse(_summary_lacks_context(summary))
                self.assertEqual(_ensure_summary_context(summary, "另一条完整新闻标题"), summary)

    def test_no_information_is_invented_when_the_title_is_also_vague(self):
        summary = "特朗普立即作出反应。"
        self.assertEqual(_ensure_summary_context(summary, "总统迅速回应"), summary)
        self.assertEqual(_ensure_summary_context(summary, "Trump responds immediately"), summary)

    def test_a_long_title_is_not_blindly_copied_or_cut(self):
        summary = "特朗普立即作出反应。"
        self.assertEqual(_ensure_summary_context(summary, "原文背景" * 30), summary)
