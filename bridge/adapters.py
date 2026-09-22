from pathlib import Path


def apply_plugin_config(settings, plugin_config, data_dir: Path):
    settings.database_path = str(data_dir / "news.db")
    settings.newsletter_settings = {
        **settings.newsletter_settings, "output_dir": str(data_dir / "output")
    }
    defaults = {
        "ai_provider": "deepseek", "ai_api_key": "", "ai_api_base": "",
        "ai_model": "", "ai_translate_enabled": True, "http_proxy": "",
    }
    for key, default in defaults.items():
        value = plugin_config.get(key, default)
        setattr(settings, key, default if value is None else value)
