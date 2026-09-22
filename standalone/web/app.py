from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import sqlite3
import os
import asyncio
import uuid
import time
import shutil
import subprocess
import tempfile
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from ...core.config.config import settings
from ...core.storage.storage import NewsStorage
from ...core.collector.collector import NewsCollector
from ...core.filter.filter import AIFilter, AITranslator, AIClient
from ...core.newsletter.newsletter import NewsletterGenerator
from ...core.notifier.notifier import EmailSender
from ...standalone.scheduler.scheduler import NewsScheduler
import logging

logger = logging.getLogger(__name__)

app = FastAPI(title="每日新闻流管理系统", version=settings.system_version)
storage = NewsStorage()

scheduler = NewsScheduler()

import json

task_status: Dict[str, Dict[str, Any]] = {}


def _init_task_table():
    try:
        with sqlite3.connect(storage.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS task_status (
                    task_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER DEFAULT 0,
                    message TEXT DEFAULT '',
                    result TEXT,
                    started_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            ''')
            conn.commit()
    except Exception as e:
        logger.error(f"初始化任务表时出错: {str(e)}")


_init_task_table()


@app.on_event("startup")
async def startup_event():
    scheduler.start()
    logger.info("Web 后台启动，定时任务调度器已启动")


@app.on_event("shutdown")
async def shutdown_event():
    scheduler.stop()
    logger.info("Web 后台关闭，定时任务调度器已停止")


class FilterRuleCreate(BaseModel):
    name: str
    type: str
    value: str
    priority: int = 1


class FilterRuleUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    value: Optional[str] = None
    priority: Optional[int] = None


class AIConfigUpdate(BaseModel):
    provider: Optional[str] = None
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    model: Optional[str] = None
    translate_enabled: Optional[bool] = None


class EmailConfigUpdate(BaseModel):
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    sender: Optional[str] = None
    recipients: Optional[str] = None
    use_ssl: Optional[bool] = None


class NewsletterRenderRequest(BaseModel):
    content: str


def _find_chrome_executable() -> str:
    configured = os.environ.get("NEWSFLOW_CHROME_PATH")
    candidates = [
        configured,
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("未找到 Chrome/Chromium。请设置 NEWSFLOW_CHROME_PATH 后重试。")


def _render_newsletter_png(content: str) -> bytes:
    if not content or len(content) > 2_000_000:
        raise ValueError("简报内容为空或超过渲染大小限制")

    chrome = _find_chrome_executable()
    summaries = re.findall(
        r'<a[^>]*class=["\']brief-summary["\'][^>]*>(.*?)</a>',
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if summaries:
        estimated_items_height = 0
        for summary in summaries:
            text = re.sub(r'<[^>]+>', '', summary).strip()
            estimated_lines = max(1, (len(text) + 21) // 22)
            estimated_items_height += 58 + estimated_lines * 34
        viewport_height = max(360, 230 + estimated_items_height)
    else:
        legacy_items = max(content.count('class="news-item"'), content.count("class='news-item'"), 1)
        viewport_height = 260 + legacy_items * 220
    viewport_height = min(viewport_height, 12_000)

    with tempfile.TemporaryDirectory(prefix="newsflow-render-") as temp_dir:
        temp_path = Path(temp_dir)
        html_path = temp_path / "newsletter.html"
        image_path = temp_path / "newsletter.png"
        html_path.write_text(content, encoding="utf-8")
        command = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={temp_path / 'profile'}",
            "--force-device-scale-factor=2",
            f"--window-size=540,{viewport_height}",
            f"--screenshot={image_path}",
            html_path.as_uri(),
        ]
        completed = subprocess.run(command, capture_output=True, timeout=45, check=False)
        if completed.returncode != 0 or not image_path.is_file():
            details = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Chrome 图片渲染失败: {details or completed.returncode}")
        return image_path.read_bytes()


def _create_task(task_type: str) -> str:
    task_id = str(uuid.uuid4())[:8]
    now = time.time()
    task_status[task_id] = {
        "type": task_type,
        "status": "running",
        "progress": 0,
        "message": "任务已启动",
        "result": None,
        "started_at": now
    }
    try:
        with sqlite3.connect(storage.db_path) as conn:
            conn.execute(
                'INSERT INTO task_status (task_id, task_type, status, progress, message, result, started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (task_id, task_type, "running", 0, "任务已启动", None, now, now)
            )
            conn.commit()
    except Exception:
        pass
    return task_id


def _update_task(task_id: str, progress: int, message: str, result: Any = None, status: str = "running"):
    now = time.time()
    if task_id in task_status:
        task_status[task_id].update({
            "progress": progress,
            "message": message,
            "result": result,
            "status": status
        })
    try:
        result_json = json.dumps(result) if result else None
        with sqlite3.connect(storage.db_path) as conn:
            conn.execute(
                'UPDATE task_status SET status=?, progress=?, message=?, result=?, updated_at=? WHERE task_id=?',
                (status, progress, message, result_json, now, task_id)
            )
            conn.commit()
    except Exception:
        pass


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(html_path):
        with open(html_path, 'r', encoding='utf-8') as f:
            return f.read()
    return "<h1>每日新闻流管理系统</h1><p>页面未找到</p>"


@app.get("/api/status")
async def get_status():
    try:
        stats = storage.get_news_stats()
        ai_client = AIClient()
        email_sender = EmailSender()

        return {
            "system_name": settings.system_name,
            "version": settings.system_version,
            "news_count": stats['total'],
            "rules_count": _get_rules_count(),
            "newsletters_count": _get_newsletters_count(),
            "source_stats": stats['by_source'],
            "category_stats": stats['by_category'],
            "translated_count": stats['translated'],
            "sources_configured": len(settings.news_sources),
            "ai_configured": ai_client.is_configured(),
            "ai_provider": ai_client.provider,
            "ai_model": ai_client.model,
            "translate_enabled": settings.ai_translate_enabled,
            "email_configured": email_sender.is_configured(),
            "scheduler_running": scheduler.scheduler.running if hasattr(scheduler, 'scheduler') else False,
            "schedule_time": f"{settings.collect_hour:02d}:{settings.collect_minute:02d}",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _get_rules_count() -> int:
    try:
        with sqlite3.connect(storage.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM filter_rules')
            return cursor.fetchone()[0]
    except:
        return 0


def _get_newsletters_count() -> int:
    try:
        with sqlite3.connect(storage.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM newsletters')
            return cursor.fetchone()[0]
    except:
        return 0


@app.get("/api/news")
async def get_news(limit: int = 50, offset: int = 0, source: Optional[str] = None, category: Optional[str] = None):
    try:
        news_list = storage.get_news(limit=limit, offset=offset, source=source, category=category)
        total = storage.get_news_stats()['total']
        return {"news": news_list, "total": total, "limit": limit, "offset": offset}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/news/search")
async def search_news(start_date: Optional[str] = None, end_date: Optional[str] = None, category: Optional[str] = None, source: Optional[str] = None, limit: int = 200):
    try:
        if start_date and end_date:
            return storage.get_news_by_date_range(start_date, end_date, category, source, limit)
        return storage.get_news(limit=limit, source=source, category=category)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/news/daily-stats")
async def get_daily_stats(days: int = 30):
    try:
        return storage.get_daily_stats(days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/news/categories")
async def get_categories():
    try:
        stats = storage.get_news_stats()
        return {"categories": stats['by_category'], "order": settings.category_order}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/task/{task_id}")
async def get_task_status(task_id: str):
    task = task_status.get(task_id)
    if not task:
        try:
            with sqlite3.connect(storage.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM task_status WHERE task_id = ?', (task_id,))
                row = cursor.fetchone()
                if row:
                    task = {
                        "type": row['task_type'],
                        "status": row['status'],
                        "progress": row['progress'],
                        "message": row['message'],
                        "result": json.loads(row['result']) if row['result'] else None,
                        "started_at": row['started_at']
                    }
                    task_status[task_id] = task
        except Exception:
            pass

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task and task.get('status') in ('completed', 'failed'):
        elapsed = time.time() - task['started_at']
        if elapsed > 3600:
            try:
                with sqlite3.connect(storage.db_path) as conn:
                    conn.execute('DELETE FROM task_status WHERE task_id = ?', (task_id,))
                    conn.commit()
            except Exception:
                pass
            if task_id in task_status:
                del task_status[task_id]
            raise HTTPException(status_code=404, detail="任务记录已过期")

    return task


async def _run_in_background(func, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func, *args, **kwargs)


@app.post("/api/collect")
async def collect_news():
    task_id = _create_task("collect")

    async def do_collect():
        try:
            _update_task(task_id, 10, "正在采集新闻...")
            collector = NewsCollector()
            news = await _run_in_background(collector.collect_news)
            _update_task(task_id, 80, f"采集到 {len(news)} 条，正在保存...")
            saved, skipped = storage.save_news(news)
            _update_task(task_id, 100, f"采集完成: 共采集 {len(news)} 条, 新增 {saved} 条, 跳过 {skipped} 条重复",
                         {"collected": len(news), "saved": saved, "skipped": skipped}, "completed")
        except Exception as e:
            _update_task(task_id, 100, f"采集失败: {str(e)}", None, "failed")

    asyncio.create_task(do_collect())
    return {"status": "ok", "task_id": task_id, "message": "采集任务已启动"}


@app.post("/api/filter")
async def filter_news():
    task_id = _create_task("filter")

    async def do_filter():
        try:
            _update_task(task_id, 10, "正在加载新闻...")
            news_list = storage.get_news(limit=200)
            if not news_list:
                _update_task(task_id, 100, "生成失败：当前数据库没有新闻，请先执行采集。",
                             {"total": 0, "filtered": 0, "reason": "no_source_news"}, "failed")
                return
            _update_task(task_id, 30, f"正在AI筛选 {len(news_list)} 条新闻...")
            ai_filter = AIFilter()
            filtered = await ai_filter.filter_news_async(news_list)
            if not filtered:
                _update_task(task_id, 100, "生成失败：没有符合条件且在有效期内的新闻，请先采集最新新闻后再试。",
                             {"total": len(news_list), "filtered": 0, "reason": "no_eligible_news"}, "failed")
                return
            _update_task(task_id, 100, f"筛选完成: {len(news_list)} 条中筛选出 {len(filtered)} 条",
                         {"total": len(news_list), "filtered": len(filtered)}, "completed")
        except Exception as e:
            _update_task(task_id, 100, f"筛选失败: {str(e)}", None, "failed")

    asyncio.create_task(do_filter())
    return {"status": "ok", "task_id": task_id, "message": "筛选任务已启动"}


@app.post("/api/translate")
async def translate_news():
    task_id = _create_task("translate")

    async def do_translate():
        try:
            _update_task(task_id, 10, "正在加载外媒新闻...")
            news_list = storage.get_news(limit=200, category="英文")
            if not news_list:
                _update_task(task_id, 100, "没有需要翻译的外媒新闻", {"translated": 0}, "completed")
                return

            translator = AITranslator()
            if not translator.is_available():
                _update_task(task_id, 100, "翻译功能未启用或未配置AI", None, "failed")
                return

            _update_task(task_id, 30, f"正在翻译 {len(news_list)} 条外媒新闻...")
            translated_list = await translator.translate_news_async(news_list)
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

            _update_task(task_id, 100, f"翻译完成: {translated_count} 条",
                         {"translated": translated_count}, "completed")
        except Exception as e:
            _update_task(task_id, 100, f"翻译失败: {str(e)}", None, "failed")

    asyncio.create_task(do_translate())
    return {"status": "ok", "task_id": task_id, "message": "翻译任务已启动"}


@app.post("/api/generate-newsletter")
async def generate_newsletter():
    task_id = _create_task("newsletter")

    async def do_generate():
        try:
            _update_task(task_id, 10, "正在加载新闻...")
            news_list = storage.get_news(limit=200)
            _update_task(task_id, 30, f"正在AI筛选 {len(news_list)} 条新闻...")
            ai_filter = AIFilter()
            filtered = await ai_filter.filter_news_async(news_list)

            translator = AITranslator()
            if translator.is_available():
                _update_task(task_id, 60, f"正在翻译 {len(filtered)} 条筛选新闻...")
                filtered = await translator.translate_news_async(filtered)

            _update_task(task_id, 80, "正在生成简报...")
            generator = NewsletterGenerator()
            path = await _run_in_background(generator.generate, filtered)
            if not path:
                _update_task(task_id, 100, "生成失败：简报未能保存，请查看服务日志。",
                             {"total": len(news_list), "filtered": len(filtered), "reason": "newsletter_generation_failed"}, "failed")
                return
            _update_task(task_id, 100, f"简报已生成, 包含 {len(filtered)} 条新闻",
                         {"path": path, "news_count": len(filtered)}, "completed")
        except Exception as e:
            _update_task(task_id, 100, f"生成简报失败: {str(e)}", None, "failed")

    asyncio.create_task(do_generate())
    return {"status": "ok", "task_id": task_id, "message": "简报生成任务已启动"}


@app.post("/api/run-daily")
async def run_daily_task():
    task_id = _create_task("daily")

    async def do_daily():
        try:
            _update_task(task_id, 5, "步骤1/7: 正在采集新闻...")
            collector = NewsCollector()
            news = await _run_in_background(collector.collect_news)
            if not news:
                _update_task(task_id, 100, "每日任务失败：未采集到新闻，未生成简报。",
                             {"collected": 0, "filtered": 0, "reason": "no_source_news"}, "failed")
                return
            saved, skipped = storage.save_news(news)
            _update_task(task_id, 20, f"步骤2/7: 采集完成(新增{saved}条, 跳过{skipped}条)，正在AI筛选...")

            ai_filter = AIFilter()
            all_news = storage.get_news(limit=200)
            filtered = await ai_filter.filter_news_async(all_news)
            if not filtered:
                _update_task(task_id, 100, "每日任务失败：没有符合条件且在有效期内的新闻，未生成简报。",
                             {"collected": len(news), "saved": saved, "filtered": 0, "reason": "no_eligible_news"}, "failed")
                return
            _update_task(task_id, 50, f"步骤3/7: 筛选完成({len(filtered)}条)，正在翻译...")

            translator = AITranslator()
            if translator.is_available():
                filtered = await translator.translate_news_async(filtered)
                for n in filtered:
                    if n.get('title_original'):
                        storage.update_translation(
                            n['id'], n['title'], n.get('summary', ''),
                            n['title_original'], n.get('summary_original', '')
                        )
            _update_task(task_id, 70, "步骤4/7: 翻译完成，正在生成简报...")

            generator = NewsletterGenerator()
            path = await _run_in_background(generator.generate, filtered)
            if not path:
                _update_task(task_id, 100, "每日任务失败：简报未能保存。",
                             {"collected": len(news), "saved": saved, "filtered": len(filtered), "reason": "newsletter_generation_failed"}, "failed")
                return
            _update_task(task_id, 85, "步骤5/7: 简报已生成，正在检查邮件推送...")

            email_sender = EmailSender()
            email_sent = False
            if email_sender.is_configured():
                tz = timezone(timedelta(hours=8))
                date_str = datetime.now(tz).strftime('%Y-%m-%d')
                newsletter = storage.get_newsletter(date_str)
                if newsletter and newsletter.get('content'):
                    email_sent = email_sender.send_newsletter(newsletter['content'], date_str=date_str)

            _update_task(task_id, 95, "步骤6/7: 正在清理旧新闻...")
            storage.clean_old_news(days=7)

            _update_task(task_id, 100,
                         f"每日任务完成: 采集{saved}条, 筛选{len(filtered)}条, 简报已生成{', 邮件已推送' if email_sent else ''}",
                         {"collected": len(news), "saved": saved, "filtered": len(filtered),
                          "newsletter_path": path, "email_sent": email_sent},
                         "completed")
        except Exception as e:
            _update_task(task_id, 100, f"每日任务失败: {str(e)}", None, "failed")

    asyncio.create_task(do_daily())
    return {"status": "ok", "task_id": task_id, "message": "每日任务已启动"}


@app.get("/api/filter-rules")
async def get_filter_rules():
    try:
        with sqlite3.connect(storage.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM filter_rules ORDER BY priority DESC, id ASC')
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/filter-rules")
async def create_filter_rule(rule: FilterRuleCreate):
    try:
        with sqlite3.connect(storage.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO filter_rules (name, type, value, priority) VALUES (?, ?, ?, ?)',
                (rule.name, rule.type, rule.value, rule.priority)
            )
            conn.commit()
            return {"status": "ok", "id": cursor.lastrowid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/filter-rules/{rule_id}")
async def update_filter_rule(rule_id: int, rule: FilterRuleUpdate):
    try:
        with sqlite3.connect(storage.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM filter_rules WHERE id = ?', (rule_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="规则不存在")

            updates = []
            values = []
            for field, value in rule.model_dump(exclude_unset=True).items():
                if field == 'link_patterns':
                    continue
                updates.append(f"{field} = ?")
                values.append(value)

            if not updates:
                return {"status": "ok"}

            values.append(rule_id)
            cursor.execute(f'UPDATE filter_rules SET {", ".join(updates)} WHERE id = ?', values)
            conn.commit()
            return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/filter-rules/{rule_id}")
async def delete_filter_rule(rule_id: int):
    try:
        with sqlite3.connect(storage.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM filter_rules WHERE id = ?', (rule_id,))
            conn.commit()
            return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/news-sources")
async def get_news_sources():
    return settings.resolved_news_sources()


@app.get("/api/ai-config")
async def get_ai_config():
    ai_client = AIClient()
    return {
        "provider": ai_client.provider,
        "api_key_set": bool(ai_client.api_key),
        "api_base": ai_client.api_base,
        "model": ai_client.model,
        "translate_enabled": settings.ai_translate_enabled,
        "is_configured": ai_client.is_configured(),
    }


@app.put("/api/ai-config")
async def update_ai_config(config: AIConfigUpdate):
    try:
        if config.provider is not None:
            settings.ai_provider = config.provider
        if config.api_key is not None:
            settings.ai_api_key = config.api_key
        if config.api_base is not None:
            settings.ai_api_base = config.api_base
        if config.model is not None:
            settings.ai_model = config.model

        ai_client = AIClient()
        return {
            "status": "ok",
            "provider": ai_client.provider,
            "api_key_set": bool(ai_client.api_key),
            "api_base": ai_client.api_base,
            "model": ai_client.model,
            "is_configured": ai_client.is_configured(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/email-config")
async def get_email_config():
    email_sender = EmailSender()
    return {
        "smtp_host": email_sender.smtp_host,
        "smtp_port": email_sender.smtp_port,
        "smtp_user": email_sender.smtp_user,
        "sender": email_sender.sender,
        "recipients": ', '.join(email_sender.recipients) if email_sender.recipients else '',
        "use_ssl": email_sender.use_ssl,
        "is_configured": email_sender.is_configured(),
    }


@app.put("/api/email-config")
async def update_email_config(config: EmailConfigUpdate):
    try:
        if config.smtp_host is not None:
            os.environ['SMTP_HOST'] = config.smtp_host
        if config.smtp_port is not None:
            os.environ['SMTP_PORT'] = str(config.smtp_port)
        if config.smtp_user is not None:
            os.environ['SMTP_USER'] = config.smtp_user
        if config.smtp_password is not None:
            os.environ['SMTP_PASSWORD'] = config.smtp_password
        if config.sender is not None:
            os.environ['EMAIL_SENDER'] = config.sender
        if config.recipients is not None:
            os.environ['EMAIL_RECIPIENTS'] = config.recipients

        email_sender = EmailSender()
        return {
            "status": "ok",
            "is_configured": email_sender.is_configured(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/email-test")
async def test_email():
    try:
        email_sender = EmailSender()
        if not email_sender.is_configured():
            return {"status": "error", "message": "邮件推送未配置"}
        result = email_sender.send_test()
        if result:
            return {"status": "ok", "message": "测试邮件已发送"}
        else:
            return {"status": "error", "message": "测试邮件发送失败"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/clean")
async def clean_old_news(days: int = 7):
    try:
        deleted = storage.clean_old_news(days=days)
        return {"status": "ok", "deleted": deleted}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/newsletters")
async def get_newsletters():
    try:
        with sqlite3.connect(storage.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT id, date, title, generated_at, format FROM newsletters ORDER BY date DESC LIMIT 30')
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/newsletters/{date}")
async def get_newsletter(date: str):
    newsletter = storage.get_newsletter(date)
    if not newsletter:
        raise HTTPException(status_code=404, detail="简报不存在")
    return newsletter


@app.get("/api/newsletters/{date}/html", response_class=HTMLResponse)
async def get_newsletter_html(date: str):
    newsletter = storage.get_newsletter(date)
    if not newsletter:
        raise HTTPException(status_code=404, detail="简报不存在")
    return newsletter.get('content', '')


@app.post("/api/render/newsletter")
async def render_newsletter(request: NewsletterRenderRequest):
    try:
        png = await asyncio.to_thread(_render_newsletter_png, request.content)
        return Response(
            content=png,
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=503, detail=str(e))
