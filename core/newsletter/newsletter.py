import os
import re
from html import escape
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from urllib.parse import urlsplit
from ...core.config.config import settings
from ...core.storage.storage import NewsStorage
import logging

logger = logging.getLogger(__name__)


class NewsletterGenerator:

    def __init__(self):
        self.storage = NewsStorage()
        self.output_dir = settings.newsletter_settings.get('output_dir', 'output')
        self._ensure_output_directory()

    def _ensure_output_directory(self):
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir, exist_ok=True)

    def generate(self, news_list: List[Dict[str, Any]], date_str: Optional[str] = None) -> Optional[str]:
        if not news_list:
            logger.warning("没有新闻可供生成简报")
            return None

        tz = timezone(timedelta(hours=8))
        if not date_str:
            date_str = datetime.now(tz).strftime('%Y-%m-%d')

        title_template = settings.newsletter_settings.get('title_template', '每日新闻简报 - {date}')
        title = title_template.format(date=date_str)

        categorized = self._categorize_news(news_list)

        now = datetime.now(tz)
        html = self._generate_html(title, date_str, categorized, len(news_list))

        output_path = os.path.join(self.output_dir, f'newsletter_{date_str}.html')
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(html)
            logger.info(f"简报已生成: {output_path}")
        except Exception as e:
            logger.error(f"保存简报文件时出错: {str(e)}")
            return None

        try:
            newsletter_id = self.storage.save_newsletter({
                'date': date_str,
                'title': title,
                'content': html,
                'format': 'html',
                'generated_at': now.isoformat()
            })
            if newsletter_id is None:
                logger.error("简报未能保存到数据库")
                return None
            self.storage.remember_published_events(news_list)
        except Exception as e:
            logger.error(f"保存简报到数据库或记录事件记忆时出错: {str(e)}")

        return output_path

    def _categorize_news(self, news_list: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        categories = {}
        for news in news_list:
            cat = news.get('category', '未分类')
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(news)

        return categories

    def _generate_simple_summary(self, news: Dict[str, Any]) -> str:
        summary = (news.get('summary', '') or '').strip()
        return self._format_summary(summary or news.get('title', '无标题'))

    @staticmethod
    def _format_summary(value: str) -> str:
        """摘要生成靠 AI；超长或缺失 AI 摘要时按句界做可观察的展示兜底。"""
        text = re.sub(r'\s+', ' ', str(value or '')).strip()
        limit = 100
        if len(text) <= limit:
            return text

        logger.warning("展示摘要超过 %d 字，按句界截取（原长 %d）", limit, len(text))
        # 优先保留完整句子；英文句号不能误切利率和金额中的小数点。
        prefix = text[:limit]
        endings = list(re.finditer(r'[。！？!?]|\.(?=\s|$)', prefix))
        endings = [match for match in endings if match.end() >= 30]
        if endings:
            return prefix[:endings[-1].end()]
        prefix = text[:limit - 1]
        clauses = list(re.finditer(r'[，,；;：:]', prefix))
        clauses = [match for match in clauses if match.start() >= 30]
        if clauses:
            prefix = prefix[:clauses[-1].start()]
        else:
            # 无标点的长英文仍尽量保持完整单词。
            word_end = prefix.rfind(' ')
            if word_end >= 30:
                prefix = prefix[:word_end]
        return prefix.rstrip(' ，,；;：:') + '…'

    def _generate_html(
        self,
        title: str,
        date_str: str,
        categorized: Dict[str, List[Dict[str, Any]]],
        total_count: int
    ) -> str:
        all_news = []
        for cat, items in categorized.items():
            all_news.extend(items)
        all_news.sort(key=lambda x: x.get('published', '') or '', reverse=True)

        items_html = ''
        for news in all_news:
            source = str(news.get('source', '未知') or '未知')
            published = str(news.get('published', '') or '')
            topic = str(news.get('topic', '其他') or '其他')
            summary = self._format_summary(
                news.get('ai_summary', '') or self._generate_simple_summary(news)
            )
            link = str(news.get('link', '') or '')
            is_external_link = urlsplit(link).scheme.lower() in {'http', 'https'}
            if is_external_link:
                summary_html = (
                    f'<a class="brief-summary" href="{escape(link, quote=True)}" '
                    f'target="_blank" rel="noopener">{escape(summary)}</a>'
                )
            else:
                summary_html = f'<span class="brief-summary">{escape(summary)}</span>'
            source_initial = escape(source[0]) if source else '新'

            items_html += f'''
                <article class="brief-card">
                    <div class="source-mark">{source_initial}</div>
                    <div class="brief-content">
                        <div class="brief-meta">
                            <span class="brief-source">{escape(source)}</span>
                            <span class="brief-topic">{escape(topic)}</span>
                            <span class="brief-time">{escape(published)}</span>
                        </div>
                        {summary_html}
                    </div>
                </article>'''

        tz = timezone(timedelta(hours=8))
        now = datetime.now(tz)

        html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{escape(title)}</title>
    <style>
        :root {{ color-scheme: light; }}
        * {{ box-sizing: border-box; }}
        body {{ margin: 0; background: #e9edf2; color: #172236; font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", system-ui, sans-serif; }}
        .container {{ width: min(100%, 540px); min-height: 100vh; margin: 0 auto; background: #f8fafc; padding-bottom: 28px; }}
        header {{ background: #234a70; color: #ffffff; padding: 30px 28px 26px; border-bottom: 6px solid #e6b25c; }}
        .eyebrow {{ display: flex; align-items: center; gap: 8px; margin-bottom: 10px; font-size: 14px; font-weight: 700; letter-spacing: 0; color: #d9e8f6; }}
        .eyebrow::before {{ content: ""; width: 9px; height: 9px; border-radius: 50%; background: #e6b25c; }}
        header h1 {{ margin: 0; font-size: 27px; line-height: 1.3; letter-spacing: 0; }}
        header .date {{ margin-top: 10px; font-size: 15px; line-height: 1.5; color: #d9e8f6; }}
        main {{ padding: 18px 16px 0; }}
        .brief-card {{ display: flex; align-items: flex-start; gap: 12px; padding: 16px 0; border-bottom: 1px solid #d8e0e8; }}
        .brief-card:last-child {{ border-bottom: 0; }}
        .source-mark {{ display: flex; align-items: center; justify-content: center; width: 42px; height: 42px; flex: 0 0 42px; border-radius: 8px; background: #dce9f4; color: #234a70; font-size: 19px; font-weight: 800; }}
        .brief-content {{ min-width: 0; flex: 1; }}
        .brief-meta {{ display: flex; align-items: baseline; gap: 9px; margin-bottom: 8px; }}
        .brief-source {{ color: #234a70; font-size: 17px; font-weight: 750; }}
        .brief-topic {{ padding: 2px 7px; border-radius: 999px; background: #edf3f8; color: #4c6278; font-size: 12px; font-weight: 700; white-space: nowrap; }}
        .brief-time {{ color: #697789; font-size: 13px; white-space: nowrap; }}
        .brief-summary {{ display: block; color: #172236; font-size: 20px; font-weight: 550; line-height: 1.68; letter-spacing: 0; text-decoration: none; }}
        .brief-summary:hover {{ color: #1d5c93; }}
        footer {{ margin: 22px 16px 0; padding-top: 16px; border-top: 1px solid #d8e0e8; text-align: center; color: #7d8998; font-size: 13px; }}
        @media (max-width: 390px) {{
            header {{ padding: 26px 20px 22px; }}
            header h1 {{ font-size: 25px; }}
            main {{ padding: 14px 14px 0; }}
            .brief-summary {{ font-size: 19px; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="eyebrow">NEWSFLOW DAILY BRIEF</div>
            <h1>{escape(title)}</h1>
            <div class="date">{now.strftime("%Y-%m-%d %H:%M")} (UTC+8) · 共 {total_count} 条精选</div>
        </header>
        <main data-news-count="{total_count}">
            {items_html}
        </main>
        <footer>由每日新闻流系统自动生成</footer>
    </div>
</body>
</html>'''

        return html
