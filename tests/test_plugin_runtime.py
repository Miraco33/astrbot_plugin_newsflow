import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class FakeCron:
    def __init__(self):
        self.jobs = {}
        self.counter = 0

    async def list_jobs(self):
        await asyncio.sleep(0)
        return list(self.jobs.values())

    async def add_basic_job(self, **kwargs):
        self.counter += 1
        job = SimpleNamespace(job_id=str(self.counter), **kwargs)
        self.jobs[job.job_id] = job
        return job

    async def delete_job(self, job_id):
        self.jobs.pop(job_id, None)


@unittest.skipUnless(importlib.util.find_spec("astrbot"), "Requires the AstrBot runtime")
class PluginRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_initialization_and_immediate_unload_leave_one_or_zero_jobs(self):
        from astrbot_plugin_newsflow.main import NewsflowPlugin, StarTools

        cron = FakeCron()
        context = SimpleNamespace(cron_manager=cron, register_web_api=lambda *args: None)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(StarTools, "get_data_dir", return_value=Path(directory)):
                for _ in range(3):
                    plugin = NewsflowPlugin(context, {"cron_expression": "0 6 * * *"})
                    await plugin.terminate()
                    self.assertEqual(len(cron.jobs), 0)
                plugin = NewsflowPlugin(context, {"cron_expression": "0 6 * * *"})
                await plugin._cron_registration
                self.assertEqual(len(cron.jobs), 1)
                done = asyncio.Event()
                plugin._work.start(done.wait())
                unloading = asyncio.create_task(plugin.terminate())
                await asyncio.sleep(0)
                self.assertFalse(unloading.done())
                done.set()
                await asyncio.wait_for(unloading, 3)
                self.assertEqual(len(cron.jobs), 0)
