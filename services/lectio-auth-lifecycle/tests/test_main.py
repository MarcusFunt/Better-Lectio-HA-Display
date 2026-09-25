import asyncio
import threading
from types import SimpleNamespace

from lectio_auth_lifecycle.main import start_browser, stop_browser


def test_concurrent_stop_waits_for_browser_start_to_finish():
    class BlockingLifecycle:
        def __init__(self):
            self.started = threading.Event()
            self.release_start = threading.Event()
            self.operations = []

        def start(self):
            self.operations.append("start-enter")
            self.started.set()
            assert self.release_start.wait(timeout=3)
            self.operations.append("start-exit")

        def stop(self):
            self.operations.append("stop")

    lifecycle = BlockingLifecycle()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(lifecycle=lifecycle, lifecycle_lock=asyncio.Lock())
        )
    )

    async def exercise_routes():
        start_task = asyncio.create_task(start_browser(request))
        assert await asyncio.to_thread(lifecycle.started.wait, 2)

        stop_task = asyncio.create_task(stop_browser(request))
        await asyncio.sleep(0)
        try:
            assert not stop_task.done()
        finally:
            lifecycle.release_start.set()

        await asyncio.gather(start_task, stop_task)

    asyncio.run(exercise_routes())

    assert lifecycle.operations == ["start-enter", "start-exit", "stop"]
