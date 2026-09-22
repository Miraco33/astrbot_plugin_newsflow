import asyncio
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from astrbot_plugin_newsflow.bridge.adapters import apply_plugin_config
from astrbot_plugin_newsflow.bridge.lifecycle import TaskSupervisor
from astrbot_plugin_newsflow.core.config.config import PluginSettings


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_rejects_new_work_and_drain_waits_for_existing_work(self):
        work = TaskSupervisor()
        done = asyncio.Event()
        task = work.start(done.wait())
        work.pause()
        with self.assertRaises(RuntimeError):
            work.start(asyncio.sleep(0))
        draining = asyncio.create_task(work.drain())
        await asyncio.sleep(0)
        self.assertFalse(draining.done())
        done.set()
        await asyncio.wait_for(draining, 2)
        self.assertTrue(task.done())
        self.assertEqual(work.active_count, 0)
        work.resume()
        await work.start(asyncio.sleep(0))

    async def test_cancelled_awaiter_cannot_hide_a_running_thread(self):
        work = TaskSupervisor()
        entered, release = threading.Event(), threading.Event()

        def blocking_work():
            entered.set()
            if not release.wait(2):
                raise TimeoutError("Test worker was not released")

        task = work.start(work.to_thread(blocking_work))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(entered.is_set())
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertGreater(work.active_count, 0)
            draining = asyncio.create_task(work.drain())
            await asyncio.sleep(0)
            self.assertFalse(draining.done())
        finally:
            release.set()
            await asyncio.wait_for(work.drain(), 3)
        await asyncio.wait_for(draining, 2)
        self.assertEqual(work.active_count, 0)


class ConfigurationTests(unittest.TestCase):
    def test_plugin_config_is_private_and_output_stays_with_data(self):
        with patch.dict(os.environ, {"AI_API_KEY": "test-other-plugin", "HTTP_PROXY": "http://other.invalid"}):
            before = dict(os.environ)
            settings = PluginSettings(_env_file=None)
            self.assertEqual(settings.ai_api_key, "")
            with TemporaryDirectory() as directory:
                data_dir = Path(directory)
                apply_plugin_config(settings, {"ai_api_key": "test-own-plugin"}, data_dir)
                self.assertEqual(settings.ai_api_key, "test-own-plugin")
                self.assertEqual(settings.database_path, str(data_dir / "news.db"))
                self.assertEqual(settings.newsletter_settings["output_dir"], str(data_dir / "output"))
            self.assertEqual(dict(os.environ), before)
