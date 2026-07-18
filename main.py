"""NewsFlow AstrBot 插件 — 每日简报 + 内置 Web 控制台"""
import asyncio
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.message_event_result import MessageChain

# ---- sys.path 注入 ----
# 1) 插件目录自身：使 bridge 子包可被 import
_plugin_dir = str(Path(__file__).parent)
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

# 2) NewsFlow 核心库根目录：使 src.xxx 可被 import
_env_root = os.environ.get("NEWSFLOW_ROOT")
if _env_root:
    _newsflow_root = Path(_env_root)
else:
    _docker_root = Path("/NewsFlow")
    if _docker_root.is_dir():
        _newsflow_root = _docker_root
    else:
        _newsflow_root = Path(r"F:\Project\NewsFlow")

_nf_root_str = str(_newsflow_root)
if _nf_root_str not in sys.path:
    sys.path.insert(0, _nf_root_str)

# ---- 配置注入 ----
from bridge.adapters import apply_plugin_config
from src.config.config import settings

PLUGIN_NAME = "astrbot_plugin_newsflow"

# ---- 任务状态追踪（内联，避免导入 app.py 触发 FastAPI 实例化） ----
_task_status: dict = {}
_task_db_table_created = False
_playwright_install_lock = asyncio.Lock()

def _init_task_table(storage_db_path: str):
    global _task_db_table_created
    if _task_db_table_created:
        return
    try:
        with sqlite3.connect(storage_db_path) as conn:
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
        _task_db_table_created = True
    except Exception:
        pass

def _create_task(task_type: str, storage_db_path: str) -> str:
    task_id = str(uuid.uuid4())[:8]
    now = time.time()
    _task_status[task_id] = {
        "type": task_type, "status": "running", "progress": 0,
        "message": "任务已启动", "result": None, "started_at": now
    }
    try:
        with sqlite3.connect(storage_db_path) as conn:
            conn.execute(
                'INSERT INTO task_status (task_id,task_type,status,progress,message,result,started_at,updated_at) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (task_id, task_type, "running", 0, "任务已启动", None, now, now)
            )
            conn.commit()
    except Exception:
        pass
    return task_id

def _update_task(task_id: str, progress: int, message: str, result=None, status: str = "running", db_path: str = ""):
    now = time.time()
    if task_id in _task_status:
        _task_status[task_id].update({
            "progress": progress, "message": message, "result": result, "status": status
        })
    if db_path:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    'UPDATE task_status SET status=?, progress=?, message=?, result=?, updated_at=? WHERE task_id=?',
                    (status, progress, message, json.dumps(result) if result else None, now, task_id)
                )
                conn.commit()
        except Exception:
            pass


class NewsflowPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        self.data_dir = StarTools.get_data_dir("astrbot_plugin_newsflow")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        apply_plugin_config(settings, self.config, self.data_dir)

        cron_expr = self.config.get("cron_expression", "0 6 * * *")
        if cron_expr:
            asyncio.create_task(self._register_cron(cron_expr))

        self._target_sessions = self.config.get("target_sessions", [])
        self._cron_job_id: str | None = None
        self._render_playwright = None
        self._render_browser = None
        self._render_lock = asyncio.Lock()

        # 初始化任务表 + 注册 Plugin Pages Web API
        _init_task_table(str(self.data_dir / "news.db"))
        self._register_web_apis()

    # ==================== Web API 注册 ====================

    def _register_web_apis(self):
        pn = PLUGIN_NAME
        ctx = self.context

        # 系统状态
        ctx.register_web_api(f"/{pn}/status",            self._web_status,               ["GET"],  "系统状态")
        # 新闻列表
        ctx.register_web_api(f"/{pn}/news",              self._web_news,                 ["GET"],  "新闻列表")
        # 每日统计
        ctx.register_web_api(f"/{pn}/daily-stats",       self._web_daily_stats,          ["GET"],  "每日统计")
        # 分类统计
        ctx.register_web_api(f"/{pn}/categories",        self._web_categories,           ["GET"],  "分类统计")
        # 简报列表
        ctx.register_web_api(f"/{pn}/newsletters",       self._web_newsletters,          ["GET"],  "简报列表")
        # 简报详情
        ctx.register_web_api(f"/{pn}/newsletters/<date>",self._web_newsletter_detail,    ["GET"],  "简报详情")
        # 触发采集
        ctx.register_web_api(f"/{pn}/collect",           self._web_collect,              ["POST"], "采集新闻")
        # 每日任务
        ctx.register_web_api(f"/{pn}/run-daily",         self._web_run_daily,            ["POST"], "执行每日任务")
        # 生成简报
        ctx.register_web_api(f"/{pn}/generate-newsletter",self._web_generate_newsletter,  ["POST"], "生成简报")
        # 补发已有简报
        ctx.register_web_api(f"/{pn}/resend-newsletter", self._web_resend_newsletter,    ["POST"], "补发今日简报")
        # 任务状态
        ctx.register_web_api(f"/{pn}/task/<task_id>",    self._web_task_status,          ["GET"],  "任务状态")
        # 定时推送目标
        ctx.register_web_api(f"/{pn}/target-sessions",   self._web_target_sessions,      ["GET", "POST"], "推送目标会话")

    @staticmethod
    def _jsonify(obj, status=200):
        try:
            from quart import jsonify as _j, make_response
            resp = _j(obj)
            resp.status_code = status
            return resp
        except ImportError:
            return obj

    # ==================== Handler: 系统状态 ====================

    async def _web_status(self):
        from src.storage.storage import NewsStorage
        from src.filter.filter import AIClient
        try:
            storage = NewsStorage()
            stats = storage.get_news_stats()
            ai_client = AIClient()
            return self._jsonify({
                "system_name": settings.system_name,
                "version": settings.system_version,
                "news_count": stats['total'],
                "translated_count": stats['translated'],
                "category_stats": stats['by_category'],
                "source_stats": stats['by_source'],
                "sources_configured": len(settings.news_sources),
                "ai_configured": ai_client.is_configured(),
                "ai_provider": ai_client.provider,
                "ai_model": ai_client.model,
                "translate_enabled": settings.ai_translate_enabled,
                "schedule_time": f"{settings.collect_hour:02d}:{settings.collect_minute:02d}",
            })
        except Exception as e:
            return self._jsonify({"error": str(e)}, 500)

    # ==================== Handler: 新闻列表 ====================

    async def _web_news(self):
        from quart import request as req
        from src.storage.storage import NewsStorage
        try:
            storage = NewsStorage()
            limit = req.args.get("limit", 50, type=int)
            offset = req.args.get("offset", 0, type=int)
            source = req.args.get("source")
            category = req.args.get("category")

            news_list = storage.get_news(limit=limit, offset=offset, source=source, category=category)
            total = storage.get_news_stats()['total']
            return self._jsonify({"news": news_list, "total": total, "limit": limit, "offset": offset})
        except Exception as e:
            return self._jsonify({"error": str(e)}, 500)

    # ==================== Handler: 每日统计 ====================

    async def _web_daily_stats(self):
        from quart import request as req
        from src.storage.storage import NewsStorage
        try:
            storage = NewsStorage()
            days = req.args.get("days", 30, type=int)
            data = storage.get_daily_stats(days)
            return self._jsonify(data)
        except Exception as e:
            return self._jsonify({"error": str(e)}, 500)

    # ==================== Handler: 分类统计 ====================

    async def _web_categories(self):
        from src.storage.storage import NewsStorage
        try:
            storage = NewsStorage()
            stats = storage.get_news_stats()
            return self._jsonify({
                "categories": stats['by_category'],
                "order": settings.category_order,
            })
        except Exception as e:
            return self._jsonify({"error": str(e)}, 500)

    # ==================== Handler: 简报列表 ====================

    async def _web_newsletters(self):
        db_path = str(self.data_dir / "news.db")
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('SELECT id, date, title, generated_at, format FROM newsletters ORDER BY date DESC LIMIT 30')
                rows = [dict(row) for row in cursor.fetchall()]
            return self._jsonify(rows)
        except Exception as e:
            return self._jsonify([])

    # ==================== Handler: 简报详情 ====================

    async def _web_newsletter_detail(self, date):
        from src.storage.storage import NewsStorage
        try:
            storage = NewsStorage()
            newsletter = storage.get_newsletter(date)
            if not newsletter:
                return self._jsonify({"error": "简报不存在"}, 404)
            return self._jsonify(newsletter)
        except Exception as e:
            return self._jsonify({"error": str(e)}, 500)

    # ==================== Handler: 推送目标 ====================

    async def _get_active_umos(self) -> list[str]:
        from sqlmodel import select
        from astrbot.core.db.po import ConversationV2

        async with self.context.get_db().get_db() as session:
            result = await session.execute(
                select(ConversationV2.user_id)
                .distinct()
                .order_by(ConversationV2.user_id)
            )
            return [row[0] for row in result.fetchall() if row[0]]

    async def _web_target_sessions(self):
        from quart import request as req

        if req.method == "GET":
            try:
                return self._jsonify({
                    "umos": await self._get_active_umos(),
                    "targets": self._target_sessions,
                })
            except Exception as e:
                logger.error(f"获取可选推送会话失败: {e}", exc_info=True)
                return self._jsonify({"error": f"获取会话失败: {e}"}, 500)

        try:
            payload = await req.get_json()
            sessions = payload.get("sessions", []) if isinstance(payload, dict) else []
            if not isinstance(sessions, list):
                return self._jsonify({"error": "sessions 必须为数组"}, 400)

            active_umos = set(await self._get_active_umos())
            targets = []
            seen = set()
            for entry in sessions:
                if not isinstance(entry, dict):
                    continue
                umo = str(entry.get("unified_msg_origin", "")).strip()
                if not umo or umo in seen:
                    continue
                if umo not in active_umos:
                    return self._jsonify({"error": f"会话不在可选列表中: {umo}"}, 400)
                seen.add(umo)
                targets.append({
                    "__template_key": "session",
                    "note": str(entry.get("note", "")).strip(),
                    "unified_msg_origin": umo,
                })

            self.config["target_sessions"] = targets
            save_config = getattr(self.config, "save_config", None)
            if not callable(save_config):
                return self._jsonify({"error": "插件配置不支持持久化"}, 500)
            save_config()
            self._target_sessions = targets
            return self._jsonify({"status": "ok", "targets": targets})
        except Exception as e:
            logger.error(f"保存推送目标失败: {e}", exc_info=True)
            return self._jsonify({"error": f"保存推送目标失败: {e}"}, 500)

    # ==================== Handler: 触发采集 ====================

    async def _web_collect(self):
        db_path = str(self.data_dir / "news.db")
        task_id = _create_task("collect", db_path)

        async def do_collect():
            from src.collector.collector import NewsCollector
            from src.storage.storage import NewsStorage
            try:
                _update_task(task_id, 10, "正在采集新闻...", db_path=db_path)
                collector = NewsCollector()
                news = await asyncio.to_thread(collector.collect_news)
                _update_task(task_id, 80, f"采集到 {len(news)} 条，正在保存...", db_path=db_path)
                storage = NewsStorage()
                saved, skipped = storage.save_news(news)
                _update_task(task_id, 100,
                             f"采集完成: 共采集 {len(news)} 条, 新增 {saved} 条, 跳过 {skipped} 条重复",
                             {"collected": len(news), "saved": saved, "skipped": skipped},
                             "completed", db_path)
            except Exception as e:
                _update_task(task_id, 100, f"采集失败: {str(e)}", None, "failed", db_path)

        asyncio.create_task(do_collect())
        return self._jsonify({"status": "ok", "task_id": task_id, "message": "采集任务已启动"})

    # ==================== Handler: 每日任务 ====================

    async def _web_run_daily(self):
        db_path = str(self.data_dir / "news.db")
        task_id = _create_task("daily", db_path)

        async def do_daily():
            try:
                _update_task(task_id, 10, "正在执行完整流水线...", db_path=db_path)
                from bridge.pipeline import run_pipeline

                html, path, stats = await asyncio.to_thread(run_pipeline)
                if stats.get("error"):
                    _update_task(task_id, 100, f"失败: {stats['error']}", None, "failed", db_path)
                else:
                    msg = (f"完成: 采集 {stats.get('collected', 0)} 条, "
                           f"保存 {stats.get('saved', 0)} 条, "
                           f"筛选 {stats.get('filtered', 0)} 条")
                    _update_task(task_id, 100, msg, stats, "completed", db_path)
            except Exception as e:
                _update_task(task_id, 100, f"失败: {str(e)}", None, "failed", db_path)

        asyncio.create_task(do_daily())
        return self._jsonify({"status": "ok", "task_id": task_id, "message": "每日任务已启动"})

    # ==================== Handler: 生成简报 ====================

    async def _web_generate_newsletter(self):
        db_path = str(self.data_dir / "news.db")
        task_id = _create_task("newsletter", db_path)

        async def do_generate():
            from src.storage.storage import NewsStorage
            from src.filter.filter import AIFilter, AITranslator
            from src.newsletter.newsletter import NewsletterGenerator
            try:
                _update_task(task_id, 10, "正在加载新闻...", db_path=db_path)
                storage = NewsStorage()
                news_list = storage.get_news(limit=200)
                if not news_list:
                    _update_task(
                        task_id,
                        100,
                        "生成失败：当前数据库没有新闻，请先执行采集。",
                        {"total": 0, "filtered": 0, "reason": "no_source_news"},
                        "failed",
                        db_path,
                    )
                    return
                _update_task(task_id, 30, f"正在AI筛选 {len(news_list)} 条新闻...", db_path=db_path)
                ai_filter = AIFilter()
                filtered = await ai_filter.filter_news_async(news_list)
                if not filtered:
                    _update_task(
                        task_id,
                        100,
                        "生成失败：没有符合条件且在有效期内的新闻，请先采集最新新闻后再试。",
                        {"total": len(news_list), "filtered": 0, "reason": "no_eligible_news"},
                        "failed",
                        db_path,
                    )
                    return

                translator = AITranslator()
                if translator.is_available():
                    _update_task(task_id, 60, f"正在翻译 {len(filtered)} 条筛选新闻...", db_path=db_path)
                    filtered = await translator.translate_news_async(filtered)

                _update_task(task_id, 80, "正在生成简报...", db_path=db_path)
                generator = NewsletterGenerator()
                path = await asyncio.to_thread(generator.generate, filtered)
                if not path:
                    _update_task(
                        task_id,
                        100,
                        "生成失败：简报未能保存，请查看插件日志。",
                        {"total": len(news_list), "filtered": len(filtered), "reason": "newsletter_generation_failed"},
                        "failed",
                        db_path,
                    )
                    return
                _update_task(task_id, 100,
                             f"简报已生成, 包含 {len(filtered)} 条新闻",
                             {"path": path, "news_count": len(filtered)},
                             "completed", db_path)
            except Exception as e:
                _update_task(task_id, 100, f"生成简报失败: {str(e)}", None, "failed", db_path)

        asyncio.create_task(do_generate())
        return self._jsonify({"status": "ok", "task_id": task_id, "message": "简报生成任务已启动"})

    async def _web_resend_newsletter(self):
        db_path = str(self.data_dir / "news.db")
        task_id = _create_task("resend", db_path)

        async def do_resend():
            from src.storage.storage import NewsStorage

            try:
                tz = timezone(timedelta(hours=8))
                date_str = datetime.now(tz).strftime("%Y-%m-%d")
                _update_task(task_id, 10, f"正在读取 {date_str} 的简报...", db_path=db_path)
                newsletter = NewsStorage().get_newsletter(date_str)
                if not newsletter or not newsletter.get("content"):
                    _update_task(
                        task_id,
                        100,
                        f"补发失败：{date_str} 尚无简报。",
                        {"reason": "newsletter_not_found"},
                        "failed",
                        db_path,
                    )
                    return

                _update_task(task_id, 40, "正在渲染并发送简报图片...", db_path=db_path)
                result = await self._send_newsletter_to_targets(
                    newsletter["content"],
                    date_str,
                    self._count_newsletter_items(newsletter["content"]),
                )
                message = f"补发完成：已推送至 {result['sent']} 个目标"
                if result["failed"]:
                    message += f"，{len(result['failed'])} 个目标失败"
                _update_task(task_id, 100, message, result, "completed", db_path)
            except Exception as e:
                logger.error("补发今日简报失败", exc_info=True)
                _update_task(task_id, 100, f"补发失败：{e}", None, "failed", db_path)

        asyncio.create_task(do_resend())
        return self._jsonify({"status": "ok", "task_id": task_id, "message": "简报补发任务已启动"})

    # ==================== Handler: 任务状态 ====================

    async def _web_task_status(self, task_id):
        task = _task_status.get(task_id)
        if not task:
            return self._jsonify({"error": "任务不存在"}, 404)

        if task.get('status') in ('completed', 'failed'):
            elapsed = time.time() - task['started_at']
            if elapsed > 3600:
                if task_id in _task_status:
                    del _task_status[task_id]
                return self._jsonify({"error": "任务记录已过期"}, 404)
        return self._jsonify(task)

    # ==================== Cron 定时 ====================

    async def _register_cron(self, cron_expr: str):
        try:
            jobs = await self.context.cron_manager.list_jobs()
            for job in jobs:
                if job.name == "newsflow_daily":
                    await self.context.cron_manager.delete_job(job.job_id)
                    logger.info(f"已清理旧的 cron job: {job.job_id}")

            job = await self.context.cron_manager.add_basic_job(
                name="newsflow_daily",
                cron_expression=cron_expr,
                handler=self._cron_run_pipeline,
                description="每日新闻流水线",
                persistent=False,
            )
            self._cron_job_id = job.job_id
            logger.info(f"NewsFlow 定时任务已注册: {cron_expr}")
        except Exception as e:
            logger.error(f"注册定时任务失败: {e}")

    async def _cron_run_pipeline(self):
        from bridge.pipeline import run_pipeline
        logger.info("[Cron] 开始执行每日流水线")
        html, path, stats = await asyncio.to_thread(run_pipeline)
        date_str = stats.get("date", datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"))
        if html and self._target_sessions:
            try:
                result = await self._send_newsletter_to_targets(
                    html,
                    date_str,
                    stats.get("filtered", 0),
                )
                logger.info(
                    "[Cron] 简报推送完成：成功 %s 个，失败 %s 个",
                    result["sent"],
                    len(result["failed"]),
                )
            except Exception as e:
                logger.error(f"[Cron] 简报推送失败: {e}", exc_info=True)
        elif html:
            logger.info("[Cron] 简报已生成（无推送目标）")

    # ==================== 命令处理 ====================

    def _parse_arg(self, event: AstrMessageEvent) -> str:
        text = (event.message_str or "").strip()
        for prefix in ["/简报", "简报"]:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        return text

    @filter.command("简报")
    async def briefing(self, event: AstrMessageEvent):
        logger.info(f"[简报] 命令触发! arg={self._parse_arg(event)!r}")
        arg = self._parse_arg(event)

        if not arg:
            yield event.plain_result("正在生成本地高清简报图片…")
            asyncio.create_task(self._fetch_briefing(event))
            return

        if arg == "状态":
            yield event.plain_result(self._get_status_text())
            return

        if arg == "运行":
            yield event.plain_result("🔄 正在运行流水线…")
            asyncio.create_task(self._run_pipeline_and_notify(event))
            return

        try:
            datetime.strptime(arg, "%Y-%m-%d")
        except ValueError:
            yield event.plain_result("日期格式应为 YYYY-MM-DD，例如 /简报 2026-07-11")
            return

        yield event.plain_result(f"正在生成 {arg} 的本地高清简报图片…")
        asyncio.create_task(self._fetch_briefing(event, arg))

    async def _ensure_render_browser(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "图片渲染需要 Playwright，请先安装插件依赖"
            ) from exc

        async with _playwright_install_lock:
            if self._render_browser is not None and self._render_browser.is_connected():
                return self._render_browser

            if self._render_playwright is not None:
                try:
                    await self._render_playwright.stop()
                except Exception:
                    pass
                self._render_playwright = None

            playwright = await async_playwright().start()
            executable_path = Path(playwright.chromium.executable_path)
            if not executable_path.is_file():
                logger.info(
                    "共享 Playwright Chromium 不存在，开始安装到 %s",
                    os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "默认缓存目录"),
                )
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "playwright",
                    "install",
                    "chromium",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=os.environ.copy(),
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=300
                    )
                except asyncio.TimeoutError as exc:
                    process.kill()
                    await process.wait()
                    await playwright.stop()
                    raise RuntimeError("Playwright Chromium 安装超时") from exc

                if process.returncode != 0 or not executable_path.is_file():
                    details = (stderr or stdout).decode("utf-8", errors="replace")[-2000:]
                    await playwright.stop()
                    raise RuntimeError(f"Playwright Chromium 安装失败: {details}")

            try:
                browser = await playwright.chromium.launch(headless=True)
            except Exception:
                await playwright.stop()
                raise

            self._render_playwright = playwright
            self._render_browser = browser
            logger.info("NewsFlow 图片渲染浏览器已就绪: %s", executable_path)
            return browser

    async def _render_newsletter_to_file(self, html: str, date_str: str) -> Path:
        if not html or len(html) > 2_000_000:
            raise ValueError("简报内容为空或超过渲染大小限制")

        browser = await self._ensure_render_browser()
        async with self._render_lock:
            page = await browser.new_page(
                viewport={"width": 540, "height": 800},
                device_scale_factor=2,
                color_scheme="light",
            )
            try:
                await page.set_content(html, wait_until="load", timeout=45_000)
                await page.evaluate(
                    "document.fonts ? document.fonts.ready : Promise.resolve()"
                )
                image = await page.screenshot(
                    type="png",
                    full_page=True,
                    animations="disabled",
                    timeout=45_000,
                )
            finally:
                await page.close()

        if len(image) > 20 * 1024 * 1024:
            raise RuntimeError("渲染图片超过 20 MB 限制")
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("Playwright 没有返回 PNG 图片")

        output_dir = self.data_dir / "rendered_newsletters"
        output_dir.mkdir(parents=True, exist_ok=True)
        image_path = output_dir / f"newsletter_{date_str}.png"
        temporary_path = image_path.with_suffix(".tmp")
        temporary_path.write_bytes(image)
        temporary_path.replace(image_path)
        return image_path

    @staticmethod
    def _count_newsletter_items(html: str) -> int:
        return html.count('class="brief-card"') or html.count("class='brief-card'")

    async def _send_newsletter_to_targets(self, html: str, date_str: str, news_count: int) -> dict:
        if not self._target_sessions:
            raise RuntimeError("未配置推送目标")

        image_path = await self._render_newsletter_to_file(html, date_str)
        result = {"sent": 0, "failed": []}
        for target in self._target_sessions:
            umo = str(target.get("unified_msg_origin", "")).strip()
            if not umo:
                continue
            try:
                sent = await self.context.send_message(
                    umo,
                    MessageChain([
                        Comp.Plain(f"每日简报 ({date_str}) · 精选 {news_count} 条"),
                        Comp.Image.fromFileSystem(str(image_path)),
                    ]),
                )
                if sent:
                    result["sent"] += 1
                else:
                    result["failed"].append({"umo": umo, "error": "未找到对应平台"})
                    logger.error(f"推送到 {umo} 失败：未找到对应平台")
            except Exception as e:
                result["failed"].append({"umo": umo, "error": str(e)})
                logger.error(f"推送到 {umo} 失败: {e}", exc_info=True)

        if not result["sent"]:
            raise RuntimeError("所有推送目标均发送失败")
        return result

    async def _fetch_briefing(self, event: AstrMessageEvent, date_str: str | None = None):
        from src.storage.storage import NewsStorage

        tz = timezone(timedelta(hours=8))
        date_str = date_str or datetime.now(tz).strftime('%Y-%m-%d')

        try:
            storage = NewsStorage()
            newsletter = storage.get_newsletter(date_str)
            if not newsletter or not newsletter.get('content'):
                await event.send(event.plain_result(f"{date_str} 尚无简报\n发送 /简报 运行 手动生成"))
                return

            image_path = await self._render_newsletter_to_file(newsletter["content"], date_str)
            await event.send(event.image_result(str(image_path)))
        except Exception as e:
            logger.error(f"获取简报失败: {e}", exc_info=True)
            await event.send(event.plain_result(f"获取简报失败: {e}"))

    def _get_status_text(self) -> str:
        lines = [
            "📊 NewsFlow 系统状态",
            f"• 项目根目录: {_newsflow_root}",
            f"• 数据目录: {self.data_dir}",
            f"• AI 提供商: {settings.ai_provider}",
            f"• AI 模型: {settings.ai_model}",
            f"• 定时任务: {self.config.get('cron_expression', '0 6 * * *')}",
            f"• 推送目标数: {len(self._target_sessions)}",
        ]
        return "\n".join(lines)

    async def _run_pipeline_and_notify(self, event: AstrMessageEvent):
        from bridge.pipeline import run_pipeline
        try:
            html, path, stats = await asyncio.to_thread(run_pipeline)
            date_str = stats.get("date", datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"))
            msg = (
                f"✅ 流水线完成 ({date_str})\n"
                f"采集 {stats.get('collected', 0)} 条 | "
                f"保存 {stats.get('saved', 0)} 条 | "
                f"筛选 {stats.get('filtered', 0)} 条"
            )
            if stats.get("error"):
                msg = f"❌ 流水线失败: {stats['error']}"
        except Exception as e:
            logger.error(f"流水线异常: {e}", exc_info=True)
            msg = f"❌ 流水线异常: {e}"
        await event.send(event.plain_result(msg))

    async def terminate(self):
        if self._cron_job_id:
            try:
                await self.context.cron_manager.delete_job(self._cron_job_id)
                logger.info(f"已清理 cron job: {self._cron_job_id}")
            except Exception as e:
                logger.warning(f"清理 cron job 失败: {e}")
        if self._render_browser is not None:
            try:
                await self._render_browser.close()
            except Exception:
                pass
            self._render_browser = None
        if self._render_playwright is not None:
            try:
                await self._render_playwright.stop()
            except Exception:
                pass
            self._render_playwright = None
        logger.info("NewsFlow 插件已卸载")
