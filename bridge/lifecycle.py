import asyncio
import logging

logger = logging.getLogger(__name__)


class TaskSupervisor:
    """Keep background work alive until its real worker has finished."""

    def __init__(self):
        self.accepting = True
        self._tasks = set()
        self._workers = set()

    @property
    def active_count(self):
        return sum(not task.done() for task in self._tasks | self._workers)

    def pause(self):
        self.accepting = False

    def resume(self):
        self.accepting = True

    def start(self, coroutine):
        if not self.accepting:
            coroutine.close()
            raise RuntimeError("NewsFlow is preparing for an update")
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("NewsFlow background task failed", exc_info=task.exception())

    async def to_thread(self, function, *args):
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        self._workers.add(worker)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Cancelling the awaiting coroutine cannot stop a Python thread.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
            worker.result()
            raise
        finally:
            self._workers.discard(worker)

    async def drain(self):
        self.pause()
        while pending := [task for task in self._tasks | self._workers if not task.done()]:
            await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
