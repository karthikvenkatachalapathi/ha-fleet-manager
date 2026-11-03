import asyncio
import threading
from types import SimpleNamespace
from fleet_manager import app as scheduler_app


def test_schedule_cycle_runs_in_worker_without_blocking_or_overlapping(monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    threads, delays, calls = [], [], []
    main_thread = threading.get_ident()

    class FakeSession:
        def __enter__(self):
            threads.append(threading.get_ident())
            return self
        def __exit__(self, *args):
            return False

    def cycle():
        with scheduler_app.SessionLocal():
            calls.append(1)
            entered.set()
            assert release.wait(2), 'bounded fixture release missing'
            finished.set()

    async def check():
        parked = asyncio.Event()
        async def controlled_sleep(seconds):
            delays.append(seconds)
            if seconds == 60:
                await parked.wait()
            else:
                await asyncio.sleep(0)
        # Replace only app's asyncio reference, not asyncio.sleep globally.
        monkeypatch.setattr(scheduler_app, 'asyncio', SimpleNamespace(sleep=controlled_sleep,to_thread=asyncio.to_thread))
        monkeypatch.setattr(scheduler_app, 'SessionLocal', FakeSession)
        monkeypatch.setattr(scheduler_app, 'run_schedule_cycle', cycle)
        task = asyncio.create_task(scheduler_app.schedule_loop())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_soon(heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), .5)
            assert calls == [1] and not finished.is_set()
            release.set()
            assert await asyncio.to_thread(finished.wait, 1)
            for _ in range(20):
                if len(delays) == 2:
                    break
                await asyncio.sleep(.005)
            assert delays == [10, 60]
            assert calls == [1], 'no overlapping or extra scheduler cycle'
        finally:
            release.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert threads and all(t != main_thread for t in threads)
    asyncio.run(check())
