import re
import unittest

from astrbot_plugin_newsflow.standalone.cli.cli import cli
from astrbot_plugin_newsflow.core.config.config import Settings


class ProjectContractTests(unittest.TestCase):
    def test_expected_cli_commands_are_registered(self):
        expected = {
            "collect",
            "run",
            "filter",
            "translate",
            "generate",
            "web",
            "status",
            "clean",
            "list-news",
        }
        self.assertTrue(expected.issubset(cli.commands), sorted(cli.commands))

    def test_default_system_version_uses_semantic_version_format(self):
        version = Settings.model_fields["system_version"].default
        self.assertNotEqual(version, "0.0.0")
        self.assertIsNotNone(re.fullmatch(r"\d+\.\d+\.\d+", version))

    def test_cls_uses_configurable_internal_rsshub_feed(self):
        settings = Settings()
        source = next(item for item in settings.resolved_news_sources() if item["name"] == "财联社")
        self.assertEqual(source["mode"], "rss")
        self.assertEqual(source["url"], settings.cls_rss_url)
        self.assertEqual(source["proxy_mode"], "bypass")
        self.assertTrue(source["allow_missing_link"])

    def test_reuters_is_a_direct_rss_source(self):
        settings = Settings()
        source = next(item for item in settings.resolved_news_sources() if item["name"] == "Reuters")
        self.assertEqual(source["mode"], "rss")
        self.assertTrue(source["url"].startswith("https://feeds.reuters.com/"))

    def test_default_news_sources_have_unique_names_and_valid_modes(self):
        sources = Settings.model_fields["news_sources"].default
        names = [source["name"] for source in sources]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(any(source.get("enabled") for source in sources))

        for source in sources:
            with self.subTest(source=source.get("name")):
                self.assertIn(source.get("mode"), {"rss", "scrape"})
                self.assertTrue(source.get("url"))
                self.assertIn("enabled", source)


if __name__ == "__main__":
    unittest.main()
