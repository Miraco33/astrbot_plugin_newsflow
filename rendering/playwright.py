import asyncio
import os
import sys
from pathlib import Path
from astrbot.api import logger


class NewsletterRenderer:
    def _init_renderer(self):
        self._render_playwright = None
        self._render_browser = None
        self._render_lock = asyncio.Lock()
        self._browser_lock = asyncio.Lock()

    async def _ensure_render_browser(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "图片渲染需要 Playwright，请先安装插件依赖"
            ) from exc

        async with self._browser_lock:
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

    async def _close_renderer(self):
        try:
            if self._render_browser is not None:
                await self._render_browser.close()
        finally:
            self._render_browser = None
            if self._render_playwright is not None:
                await self._render_playwright.stop()
            self._render_playwright = None
