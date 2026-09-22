from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timezone, timedelta
import logging
from ...core.collector.collector import NewsCollector
from ...core.storage.storage import NewsStorage
from ...core.filter.filter import AIFilter, AITranslator
from ...core.newsletter.newsletter import NewsletterGenerator
from ...core.notifier.notifier import EmailSender
from ...core.config.config import settings

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class NewsScheduler:

    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.collector = NewsCollector()
        self.storage = NewsStorage()

    def start(self):
        trigger = CronTrigger(
            hour=settings.collect_hour,
            minute=settings.collect_minute
        )

        self.scheduler.add_job(
            func=self._daily_task,
            trigger=trigger,
            id='daily_news_task',
            name='每日新闻采集和处理',
            replace_existing=True,
            misfire_grace_time=3600
        )

        self.scheduler.start()
        logger.info(f"调度器已启动，每日 {settings.collect_hour}:{settings.collect_minute} 执行任务")

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("调度器已停止")

    def run_now(self):
        logger.info("手动触发任务执行")
        self._daily_task()

    def _daily_task(self):
        logger.info("开始执行每日新闻任务")

        try:
            logger.info("步骤1: 采集新闻")
            news_list = self.collector.collect_news()

            if not news_list:
                logger.warning("未采集到任何新闻")
                return

            logger.info("步骤2: 存储新闻")
            saved_count, skipped_count = self.storage.save_news(news_list)

            if saved_count == 0:
                logger.warning("没有新新闻需要存储")

            logger.info("步骤3: AI筛选新闻")
            ai_filter = AIFilter()
            all_news = self.storage.get_news(limit=200)
            filtered = ai_filter.filter_news(all_news)
            logger.info(f"筛选完成: {len(all_news)} 条中筛选出 {len(filtered)} 条")

            logger.info("步骤4: AI翻译外媒新闻")
            translator = AITranslator()
            if translator.is_available():
                filtered = translator.translate_news(filtered)
                for news in filtered:
                    if news.get('title_original'):
                        self.storage.update_translation(
                            news['id'],
                            news['title'],
                            news.get('summary', ''),
                            news['title_original'],
                            news.get('summary_original', '')
                        )
                logger.info("翻译完成")
            else:
                logger.info("翻译功能未启用或未配置AI")

            # AIFilter 内部已经完成文章、事件和故事级约束；翻译后不得再次按文本去重，
            # 否则未翻译的中文条目会命中本次运行刚生成的事件标识，而英文标题变化后反而绕过去重。
            logger.info("步骤5: 使用筛选阶段的去重结果")

            logger.info("步骤6: 生成简报")
            generator = NewsletterGenerator()
            path = generator.generate(filtered)
            if path:
                logger.info(f"简报已生成: {path}")

            logger.info("步骤7: 邮件推送")
            email_sender = EmailSender()
            if email_sender.is_configured():
                tz = timezone(timedelta(hours=8))
                date_str = datetime.now(tz).strftime('%Y-%m-%d')
                newsletter = self.storage.get_newsletter(date_str)
                if newsletter and newsletter.get('content'):
                    if email_sender.send_newsletter(newsletter['content'], date_str=date_str):
                        logger.info("邮件推送成功")
                    else:
                        logger.warning("邮件推送失败")
            else:
                logger.info("邮件推送未配置，跳过")

            logger.info("步骤8: 清理旧新闻")
            deleted_count = self.storage.clean_old_news(days=7)

            logger.info(f"每日任务执行完成: 采集 {len(news_list)} 条, 保存 {saved_count} 条, 筛选 {len(filtered)} 条, 清理 {deleted_count} 条旧新闻")

        except Exception as e:
            logger.error(f"执行每日任务时出错: {str(e)}")


if __name__ == "__main__":
    import time

    scheduler = NewsScheduler()
    scheduler.start()
    scheduler.run_now()

    try:
        print("调度器正在运行，按 Ctrl+C 停止...")
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        scheduler.stop()
        print("调度器已停止")
