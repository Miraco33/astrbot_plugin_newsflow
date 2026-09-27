import unittest

from astrbot_plugin_newsflow.core.filter.filter import (
    FILTER_SYSTEM_PROMPT,
    _key_figure_numbers,
    _summary_figure_numbers,
    _summary_with_key_figures,
)
from astrbot_plugin_newsflow.core.newsletter.newsletter import NewsletterGenerator


class SummaryBudgetTests(unittest.TestCase):
    def test_calendar_dates_do_not_force_a_source_paragraph_into_summary(self):
        summary = "主管部门发布产业发展新规，明确企业申报条件、审核程序和项目实施要求，相关措施已经开始执行。"
        news = {
            "title": "产业新规公布",
            "summary": "2026年9月25日，主管部门召开新闻发布会，详细介绍新规出台的背景和实施过程，"
            "各相关单位负责人参加会议并就具体程序和企业申报条件作出说明。",
        }
        actual = _summary_with_key_figures(news, summary)
        self.assertEqual(actual, summary)
        self.assertEqual(NewsletterGenerator._format_summary(actual), summary)
        self.assertTrue(_key_figure_numbers(news["summary"]))

    def test_real_financial_figures_and_durations_are_still_detected(self):
        text = "2026年9月25日披露，2026年营收11.4亿元，增长5.1%，项目周期3年，需6个月完工。"
        figures = _summary_figure_numbers(text)
        self.assertNotIn("2026", figures)
        self.assertNotIn("9", figures)
        self.assertTrue({"11.4", "5.1", "3", "6"}.issubset(figures))

    def test_a_long_numeric_sentence_never_expands_a_short_summary(self):
        summary = "公司宣布建设新工厂，以扩大芯片制造产能并增加研发岗位。"
        news = {"title": "公司建设新工厂", "summary": "项目投资20亿元，" + "相关配套设施和建设方案仍在进一步完善，" * 8}
        with self.assertLogs("astrbot_plugin_newsflow.core.filter.filter", level="WARNING"):
            actual = _summary_with_key_figures(news, summary)
        self.assertEqual(actual, summary)
        self.assertNotIn("…", actual)

    def test_short_complete_numeric_fact_can_be_preserved_within_budget(self):
        news = {"title": "公司公布业绩", "summary": "公司营收11.4亿元，同比增长5.1%。"}
        actual = _summary_with_key_figures(news, "公司公布季度业绩，营收增长。")
        self.assertIn("11.4亿元", actual)
        self.assertIn("5.1%", actual)
        self.assertLessEqual(len(actual), 80)

    def test_a_date_in_summary_does_not_mask_a_missing_financial_figure(self):
        news = {"title": "公司公布业绩", "summary": "2026年，公司营收11.4亿元。"}
        actual = _summary_with_key_figures(news, "公司2026年营收增长。")
        self.assertIn("11.4亿元", actual)

    def test_already_preserved_figure_does_not_duplicate_original_text(self):
        summary = "公司营收11.4亿元，同比增长5.1%。"
        self.assertEqual(_summary_with_key_figures({"summary": summary}, summary), summary)

    def test_prompt_requires_counting_and_rewriting_instead_of_clipping(self):
        self.assertIn("超过80字符必须改写到80以内才可输出", FILTER_SYSTEM_PROMPT)
        self.assertIn("不得以省略号或半句话凑字数", FILTER_SYSTEM_PROMPT)
