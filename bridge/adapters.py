import os
import sys
from pathlib import Path


def _resolve_newsflow_root() -> Path:
    """按优先级确定 NewsFlow 项目根目录：
    1. NEWSFLOW_ROOT 环境变量（Docker compose 或 kubernetes 注入）
    2. /NewsFlow（Docker 默认挂载点）
    3. F:\\Project\\NewsFlow（Windows 本地开发）
    """
    env_path = os.environ.get("NEWSFLOW_ROOT")
    if env_path:
        return Path(env_path)

    docker_path = Path("/NewsFlow")
    if docker_path.is_dir():
        return docker_path

    return Path(r"F:\Project\NewsFlow")


NEWSFLOW_ROOT = _resolve_newsflow_root()

if str(NEWSFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(NEWSFLOW_ROOT))


def apply_plugin_config(settings, plugin_config, data_dir: Path):
    settings.database_path = str(data_dir / "news.db")

    if plugin_config.get("ai_api_key"):
        settings.ai_api_key = plugin_config["ai_api_key"]
    if plugin_config.get("ai_provider"):
        settings.ai_provider = plugin_config["ai_provider"]
    if plugin_config.get("ai_api_base"):
        settings.ai_api_base = plugin_config["ai_api_base"]
    if plugin_config.get("ai_model"):
        settings.ai_model = plugin_config["ai_model"]
    if plugin_config.get("ai_translate_enabled") is not None:
        settings.ai_translate_enabled = plugin_config["ai_translate_enabled"]
    if plugin_config.get("http_proxy"):
        settings.http_proxy = plugin_config["http_proxy"]

    os.environ["AI_PROVIDER"] = settings.ai_provider
    os.environ["AI_API_KEY"] = settings.ai_api_key
    os.environ["AI_API_BASE"] = settings.ai_api_base
    os.environ["AI_MODEL"] = settings.ai_model
    if settings.http_proxy:
        os.environ["HTTP_PROXY"] = settings.http_proxy
    elif "HTTP_PROXY" in os.environ:
        del os.environ["HTTP_PROXY"]
