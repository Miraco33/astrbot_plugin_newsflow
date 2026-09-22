from copy import deepcopy
from pydantic_settings import BaseSettings
from typing import List, Dict, Any


class Settings(BaseSettings):
    system_name: str = "每日新闻流"
    system_version: str = "4.3.0"

    database_path: str = "data/news.db"

    collect_hour: int = 6
    collect_minute: int = 0

    ai_provider: str = "openai"
    ai_api_key: str = ""
    ai_api_base: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    ai_translate_enabled: bool = True
    http_proxy: str = ""
    cls_rss_url: str = "http://rsshub:1200/cls/telegraph"

    news_sources: List[Dict[str, Any]] = [
        {
            "name": "新华社",
            "url": "http://www.xinhuanet.com/",
            "enabled": True,
            "category": "中文",
            "mode": "scrape",
            "link_patterns": ["/20", "politics", "world", "finance", "tech"],
            "max_articles": 20,
            "priority": 4
        },
        {
            "name": "财联社",
            "url": "http://rsshub:1200/cls/telegraph",
            "url_setting": "cls_rss_url",
            "enabled": True,
            "category": "财经",
            "mode": "rss",
            "proxy_mode": "bypass",
            "allow_missing_link": True,
            "fetch_full_content": False,
            "synthetic_link_prefix": "urn:newsflow:cls",
            "max_articles": 20,
            "priority": 5
        },
        {
            "name": "财经杂志",
            "url": "https://www.caijing.com.cn/",
            "enabled": True,
            "category": "财经",
            "mode": "scrape",
            "link_patterns": ["/20", "article"],
            "max_articles": 15,
            "priority": 4
        },
        {
            "name": "财新网",
            "url": "https://www.caixin.com/",
            "enabled": True,
            "category": "财经",
            "mode": "scrape",
            "link_patterns": ["/20", "article"],
            "max_articles": 15,
            "priority": 5
        },
        {
            "name": "BBC News",
            "url": "http://feeds.bbci.co.uk/news/rss.xml",
            "enabled": True,
            "category": "英文",
            "mode": "rss",
            "priority": 3,
            "max_articles": 20
        },
        {
            "name": "纽约时报",
            "url": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
            "enabled": True,
            "category": "英文",
            "mode": "rss",
            "priority": 4,
            "max_articles": 20
        },
        {
            "name": "Financial Times",
            "url": "https://www.ft.com/rss/home",
            "enabled": True,
            "category": "英文",
            "mode": "rss",
            "priority": 5,
            "max_articles": 20
        },
        {
            "name": "Bloomberg",
            "url": "https://feeds.bloomberg.com/markets/news.rss",
            "enabled": False,
            "category": "英文",
            "mode": "rss",
            "priority": 5,
            "max_articles": 20
        },
        {
            "name": "Reuters",
            "url": "https://feeds.reuters.com/reuters/topNews",
            "enabled": True,
            "category": "英文",
            "mode": "rss",
            "exclude_keywords": ["sport", "football", "soccer", "basketball", "tennis", "cricket", "entertainment", "celebrity", "movie", "fashion"],
            "priority": 5,
            "max_articles": 20
        },
        {
            "name": "半岛电视台",
            "url": "https://www.aljazeera.com/xml/rss/all.xml",
            "enabled": True,
            "category": "英文",
            "mode": "rss",
            "exclude_keywords": ["sport", "football", "soccer", "basketball", "tennis", "cricket", "entertainment", "celebrity", "movie"],
            "priority": 3,
            "max_articles": 20
        }
    ]

    filter_settings: Dict[str, Any] = {
        "keywords": ["科技", "金融", "国际关系", "政治"],
        "exclude_keywords": ["娱乐", "体育", "八卦"],
        "max_news": 20,
        "min_news": 16,
        "candidate_pool_multiplier": 3,
        "ai_filter_max_tokens": 4000,
        "ai_filter_split_depth": 2,
        "ai_reserve_score_min": 4,
        "per_source_max": 4,
        "per_story_max": 2,
        "topic_quotas": {
            "政策监管": 5,
            "财经市场": 4,
            "科技产业": 8,
            "国际局势": 3,
            "其他": 0
        },
        "dedup_window_hours": 72,
        "event_memory_days": 7
    }

    newsletter_settings: Dict[str, Any] = {
        "output_dir": "output",
        "formats": ["html"],
        "title_template": "每日新闻简报 - {date}"
    }

    category_order: List[str] = ["政策监管", "财经市场", "科技产业", "国际局势", "其他"]

    email_settings: Dict[str, Any] = {
        "smtp_host": "",
        "smtp_port": 465,
        "smtp_user": "",
        "smtp_password": "",
        "sender": "",
        "recipients": "",
        "use_ssl": True
    }

    def resolved_news_sources(self) -> List[Dict[str, Any]]:
        """返回应用运行时 URL 覆盖后的新闻源副本。"""
        sources = deepcopy(self.news_sources)
        for source in sources:
            setting_name = source.get("url_setting")
            if setting_name:
                configured_url = str(getattr(self, setting_name, "") or "").strip()
                if configured_url:
                    source["url"] = configured_url
        return sources

    class Config:
        env_file = "api_config.env"
        case_sensitive = False


class PluginSettings(Settings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        # AstrBot configuration must not inherit another plugin's environment.
        return (init_settings,)


settings = PluginSettings(_env_file=None)
