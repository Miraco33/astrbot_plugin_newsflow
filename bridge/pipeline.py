import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


def run_pipeline() -> tuple[Optional[str], Optional[str], dict]:
    logger.info("开始执行 NewsFlow 完整流水线")

    try:
        logger.info("步骤1/8: 采集新闻")
        from src.collector.collector import NewsCollector
        from src.storage.storage import NewsStorage

        collector = NewsCollector()
        storage = NewsStorage()
        news_list = collector.collect_news()
        if not news_list:
            logger.warning("未采集到任何新闻，未生成简报")
            return (None, None, {
                "error": "未采集到新闻，未生成简报",
                "reason": "no_source_news",
                "collected": 0,
                "filtered": 0,
            })
    except Exception as e:
        logger.error(f"采集新闻失败: {e}")
        return (None, None, {"error": f"采集新闻失败: {e}", "reason": "collect_failed"})

    try:
        logger.info("步骤2/8: 存储新闻")
        saved_count, skipped_count = storage.save_news(news_list)
    except Exception as e:
        logger.error(f"存储新闻失败: {e}")
        return (None, None, {"error": f"save failed: {e}"})

    try:
        logger.info("步骤3/8: AI 筛选新闻")
        from src.filter.filter import AIFilter, AITranslator

        ai_filter = AIFilter()
        all_news = storage.get_news(limit=200)
        filtered = ai_filter.filter_news(all_news)
        logger.info(f"筛选完成: {len(all_news)} 条中筛选出 {len(filtered)} 条")
    except Exception as e:
        logger.error(f"AI 筛选失败: {e}")
        return (None, None, {"error": f"筛选新闻失败: {e}", "reason": "filter_failed"})

    if not filtered:
        logger.warning("筛选后没有符合条件且在有效期内的新闻，未生成简报")
        return (None, None, {
            "error": "没有符合条件且在有效期内的新闻，未生成简报",
            "reason": "no_eligible_news",
            "collected": len(news_list),
            "saved": saved_count,
            "filtered": 0,
        })

    try:
        logger.info("步骤4/8: AI 翻译外媒新闻")
        translator = AITranslator()
        if translator.is_available():
            filtered = translator.translate_news(filtered)
            for news in filtered:
                if news.get('title_original'):
                    storage.update_translation(
                        news['id'],
                        news['title'],
                        news.get('summary', ''),
                        news['title_original'],
                        news.get('summary_original', '')
                    )
            logger.info("翻译完成")
        else:
            logger.info("翻译功能未启用或未配置AI")
    except Exception as e:
        logger.error(f"翻译失败（非致命）: {e}")

    # 注：_two_round_filter() 内部已包含 _deduplicate_similar + _event_deduplicate，
    # 此处不再重复去重，避免二次裁减导致简报条目过少。
    html = None
    path = None
    try:
        logger.info("步骤6/8: 生成简报")
        from src.newsletter.newsletter import NewsletterGenerator

        generator = NewsletterGenerator()
        path = generator.generate(filtered)
        if not path:
            return (None, None, {
                "error": "简报保存失败，未生成可用简报",
                "reason": "newsletter_generation_failed",
                "collected": len(news_list),
                "saved": saved_count,
                "filtered": len(filtered),
            })

        logger.info(f"简报已生成: {path}")
        tz = timezone(timedelta(hours=8))
        date_str = datetime.now(tz).strftime('%Y-%m-%d')
        newsletter = storage.get_newsletter(date_str)
        if not newsletter or not newsletter.get('content'):
            return (None, None, {
                "error": "简报未能写入数据库，未执行推送",
                "reason": "newsletter_persistence_failed",
                "collected": len(news_list),
                "saved": saved_count,
                "filtered": len(filtered),
            })
        html = newsletter['content']
    except Exception as e:
        logger.error(f"生成简报失败: {e}")
        return (None, None, {
            "error": f"生成简报失败: {e}",
            "reason": "newsletter_generation_failed",
            "collected": len(news_list),
            "saved": saved_count,
            "filtered": len(filtered),
        })

    try:
        logger.info("步骤7/8: 邮件推送")
        from src.notifier.notifier import EmailSender

        email_sender = EmailSender()
        if email_sender.is_configured():
            tz = timezone(timedelta(hours=8))
            date_str = datetime.now(tz).strftime('%Y-%m-%d')
            newsletter = storage.get_newsletter(date_str)
            if newsletter and newsletter.get('content'):
                if email_sender.send_newsletter(newsletter['content'], date_str=date_str):
                    logger.info("邮件推送成功")
                else:
                    logger.warning("邮件推送失败")
        else:
            logger.info("邮件推送未配置，跳过")
    except Exception as e:
        logger.error(f"邮件推送失败（非致命）: {e}")

    try:
        logger.info("步骤8/8: 清理旧新闻")
        deleted_count = storage.clean_old_news(days=7)
    except Exception as e:
        logger.error(f"清理旧新闻失败（非致命）: {e}")
        deleted_count = 0

    tz = timezone(timedelta(hours=8))
    date_str = datetime.now(tz).strftime('%Y-%m-%d')

    stats = {
        "collected": len(news_list),
        "saved": saved_count,
        "filtered": len(filtered),
        "cleaned": deleted_count,
        "date": date_str,
    }

    logger.info(f"流水线完成: 采集 {len(news_list)} 条, 保存 {saved_count} 条, 筛选 {len(filtered)} 条")
    return (html, path, stats)
