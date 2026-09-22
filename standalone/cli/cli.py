import click
import logging
import os
from ...standalone.scheduler.scheduler import NewsScheduler
from ...core.storage.storage import NewsStorage
from ...core.collector.collector import NewsCollector
from ...core.filter.filter import AIFilter, AITranslator, AIClient
from ...core.newsletter.newsletter import NewsletterGenerator
from ...core.config.config import settings

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@click.group()
def cli():
    """每日新闻流系统命令行工具"""
    pass


@cli.command()
def collect():
    """立即采集新闻"""
    click.echo("开始采集新闻...")

    try:
        collector = NewsCollector()
        news_list = collector.collect_news()

        if not news_list:
            click.echo("未采集到任何新闻")
            return

        storage = NewsStorage()
        saved_count, skipped_count = storage.save_news(news_list)

        click.echo(f"采集完成: 共采集 {len(news_list)} 条新闻, 新增 {saved_count} 条, 跳过 {skipped_count} 条重复")

    except Exception as e:
        click.echo(f"采集新闻时出错: {str(e)}")
        logger.error(f"采集新闻时出错: {str(e)}")


@cli.command()
def run():
    """立即执行完整的每日任务"""
    click.echo("开始执行每日任务...")

    try:
        scheduler = NewsScheduler()
        scheduler.run_now()
        click.echo("每日任务执行完成")

    except Exception as e:
        click.echo(f"执行任务时出错: {str(e)}")
        logger.error(f"执行任务时出错: {str(e)}")


@cli.command()
def filter_cmd():
    """使用AI筛选新闻"""
    click.echo("开始筛选新闻...")

    try:
        storage = NewsStorage()
        news_list = storage.get_news(limit=200)

        if not news_list:
            click.echo("没有新闻可供筛选")
            return

        ai_filter = AIFilter()
        filtered = ai_filter.filter_news(news_list)

        click.echo(f"筛选完成: {len(news_list)} 条新闻中筛选出 {len(filtered)} 条")

        for i, item in enumerate(filtered[:20]):
            score = item.get('relevance_score', '')
            reason = item.get('filter_reason', '')
            click.echo(f"\n{i+1}. {item['title']}")
            click.echo(f"   来源: {item['source']} | 分类: {item.get('category', '未分类')}")
            if score:
                click.echo(f"   相关度: {score}")
            if reason:
                click.echo(f"   理由: {reason}")

    except Exception as e:
        click.echo(f"筛选新闻时出错: {str(e)}")
        logger.error(f"筛选新闻时出错: {str(e)}")


@cli.command()
def translate():
    """翻译外媒新闻为中文"""
    click.echo("开始翻译外媒新闻...")

    try:
        translator = AITranslator()
        if not translator.is_available():
            click.echo("翻译功能未启用或未配置AI，请先配置 AI_API_KEY")
            return

        storage = NewsStorage()
        news_list = storage.get_news(limit=200, category="英文")

        if not news_list:
            click.echo("没有需要翻译的外媒新闻")
            return

        click.echo(f"找到 {len(news_list)} 条外媒新闻，开始翻译...")

        translated_list = translator.translate_news(news_list)
        translated_count = sum(1 for n in translated_list if n.get('title_original'))

        for news in translated_list:
            if news.get('title_original'):
                storage.update_translation(
                    news['id'],
                    news['title'],
                    news.get('summary', ''),
                    news['title_original'],
                    news.get('summary_original', '')
                )

        click.echo(f"翻译完成: {translated_count} 条新闻已翻译")

        for i, item in enumerate(translated_list[:10]):
            if item.get('title_original'):
                click.echo(f"\n{i+1}. {item['title']}")
                click.echo(f"   原文: {item['title_original']}")
                click.echo(f"   来源: {item['source']}")

    except Exception as e:
        click.echo(f"翻译新闻时出错: {str(e)}")
        logger.error(f"翻译新闻时出错: {str(e)}")


@cli.command()
def generate():
    """生成今日简报"""
    click.echo("开始生成简报...")

    try:
        storage = NewsStorage()
        news_list = storage.get_news(limit=200)

        if not news_list:
            click.echo("没有新闻可供生成简报")
            return

        ai_filter = AIFilter()
        filtered = ai_filter.filter_news(news_list)

        translator = AITranslator()
        if translator.is_available():
            click.echo("正在翻译外媒新闻...")
            filtered = translator.translate_news(filtered)

        generator = NewsletterGenerator()
        path = generator.generate(filtered)

        if path:
            click.echo(f"简报已生成: {path}")
            click.echo(f"包含 {len(filtered)} 条新闻")
        else:
            click.echo("简报生成失败")

    except Exception as e:
        click.echo(f"生成简报时出错: {str(e)}")
        logger.error(f"生成简报时出错: {str(e)}")


@cli.command()
@click.option('--host', default='127.0.0.1', help='Web服务主机')
@click.option('--port', default=8000, help='Web服务端口')
def web(host, port):
    """启动Web管理后台"""
    click.echo(f"启动Web管理后台: http://{host}:{port}")

    try:
        import uvicorn
        from ...standalone.web.app import app
        uvicorn.run(app, host=host, port=port)
    except ImportError:
        click.echo("uvicorn 未安装，请运行: uv pip install uvicorn")
    except Exception as e:
        click.echo(f"启动Web服务时出错: {str(e)}")
        logger.error(f"启动Web服务时出错: {str(e)}")


@cli.command()
def status():
    """查看系统状态"""
    click.echo("系统状态信息:")
    click.echo(f"系统名称: {settings.system_name}")
    click.echo(f"系统版本: {settings.system_version}")
    click.echo(f"数据库路径: {settings.database_path}")
    click.echo(f"定时任务: 每日 {settings.collect_hour:02d}:{settings.collect_minute:02d}")

    click.echo("\n新闻源配置:")
    for source in settings.resolved_news_sources():
        status_str = "启用" if source.get('enabled', False) else "禁用"
        mode = source.get('mode', 'rss')
        click.echo(f"  - {source['name']} ({status_str}, {mode}, {source.get('category', '')}): {source['url']}")

    try:
        storage = NewsStorage()
        stats = storage.get_news_stats()
        click.echo(f"\n新闻统计:")
        click.echo(f"  总数: {stats['total']}")
        click.echo(f"  已翻译: {stats['translated']}")
        click.echo(f"  按来源: {stats['by_source']}")
        click.echo(f"  按分类: {stats['by_category']}")
    except Exception as e:
        click.echo(f"获取数据库状态时出错: {str(e)}")

    ai_client = AIClient()
    click.echo(f"\nAI配置:")
    click.echo(f"  提供商: {ai_client.provider}")
    click.echo(f"  模型: {ai_client.model}")
    click.echo(f"  API Base: {ai_client.api_base}")
    click.echo(f"  状态: {'已配置' if ai_client.is_configured() else '未配置'}")
    click.echo(f"  翻译功能: {'启用' if settings.ai_translate_enabled else '禁用'}")


@cli.command()
@click.option('--days', default=7, help='保留天数')
def clean(days):
    """清理旧新闻"""
    click.echo(f"开始清理 {days} 天前的旧新闻...")

    try:
        storage = NewsStorage()
        deleted_count = storage.clean_old_news(days=days)
        click.echo(f"清理完成: 删除了 {deleted_count} 条旧新闻")

    except Exception as e:
        click.echo(f"清理旧新闻时出错: {str(e)}")
        logger.error(f"清理旧新闻时出错: {str(e)}")


@cli.command()
@click.option('--source', default=None, help='按来源过滤新闻')
@click.option('--category', default=None, help='按分类过滤新闻')
@click.option('--limit', default=10, help='显示新闻数量')
def list_news(source, category, limit):
    """查看最近的新闻"""
    click.echo("最近的新闻:")

    try:
        storage = NewsStorage()
        news = storage.get_news(limit=limit, source=source, category=category)

        if not news:
            click.echo("没有新闻")
            return

        current_cat = None
        for i, item in enumerate(news):
            cat = item.get('category', '未分类')
            if cat != current_cat:
                current_cat = cat
                click.echo(f"\n{'='*40}")
                click.echo(f"  {cat}")
                click.echo(f"{'='*40}")

            click.echo(f"\n{i+1}. {item['title']}")
            if item.get('title_original'):
                click.echo(f"   原文: {item['title_original']}")
            click.echo(f"   来源: {item['source']} | 发布: {item['published']}")
            click.echo(f"   链接: {item['link']}")
            if item.get('summary'):
                summary = item['summary'][:200]
                if len(item['summary']) > 200:
                    summary += "..."
                click.echo(f"   摘要: {summary}")

    except Exception as e:
        click.echo(f"查看新闻时出错: {str(e)}")
        logger.error(f"查看新闻时出错: {str(e)}")


if __name__ == '__main__':
    cli()
