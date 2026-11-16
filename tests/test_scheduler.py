import threading
import time

from syncalist.sync.scheduler import Scheduler


def test_runs_repeatedly_and_survives_errors():
    calls = []

    def run():
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise RuntimeError("first round fails")

    s = Scheduler(run, lambda: 0, threading.Lock(), first_delay=0)
    # interval is clamped to >= 5s; wake() skips the wait
    s.start()
    for _ in range(3):
        time.sleep(0.05)
        s.wake()
    time.sleep(0.1)
    s.stop()
    assert len(calls) >= 2
