"""Background thread that runs a sync round every N seconds."""

import logging
import threading
from collections.abc import Callable

log = logging.getLogger("syncalist.scheduler")


class Scheduler:
    def __init__(self, run_once: Callable[[], None], interval: Callable[[], int], lock: threading.Lock,
                 first_delay: float = 10.0):
        self._run_once = run_once
        self._interval = interval
        self._lock = lock
        self._first_delay = first_delay
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="syncalist-sync", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        """Re-read the interval now (e.g. after it was changed in Settings)."""
        self._wake.set()

    def _loop(self) -> None:
        self._wake.wait(self._first_delay)
        while not self._stop.is_set():
            self._wake.clear()
            try:
                with self._lock:
                    self._run_once()
            except Exception:  # noqa: BLE001 - keep the loop alive whatever happens
                log.exception("Sync round failed")
            self._wake.wait(max(5, self._interval()))
