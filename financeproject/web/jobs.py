"""Long tasks of the interface (updating prices, building the demo), one at a time, in the background.

A task gets a ``progress`` callback; the page asks for its state every second
and shows the latest messages, then the result or the error.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from financeproject.errors import FinanceError

RUNNING, DONE, FAILED = "running", "done", "failed"
KEEP_MESSAGES = 50


@dataclass
class Job:
    name: str
    status: str = RUNNING
    messages: list[str] = field(default_factory=list)
    started: datetime = field(default_factory=datetime.now)
    finished: datetime | None = None
    result: str | None = None
    error: str | None = None
    after: Callable[[], None] | None = None
    """Called on success, in the job's thread (for example, to open the ledger it built)."""

    def progress(self, message: str) -> None:
        self.messages.append(message)
        del self.messages[:-KEEP_MESSAGES]

    @property
    def last(self) -> str | None:
        return self.messages[-1] if self.messages else None

    @property
    def running(self) -> bool:
        return self.status == RUNNING


class Jobs:
    def __init__(self):
        self._lock = threading.Lock()
        self.current: Job | None = None

    def start(self, name: str, task: Callable[[Callable[[str], None]], str], after: Callable[[], None] | None = None) -> Job:
        """Run ``task(progress)`` in a thread; its return value is the job's result."""
        with self._lock:
            if self.current is not None and self.current.running:
                raise FinanceError(f"Another task is running: {self.current.name}")
            job = Job(name, after=after)
            self.current = job

        def run() -> None:
            try:
                job.result = task(job.progress)
                if job.after is not None:
                    job.after()
                job.status = DONE
            except Exception as exc:  # noqa: BLE001 - the page shows any failure
                job.error = str(exc) or exc.__class__.__name__
                job.status = FAILED
            finally:
                job.finished = datetime.now()

        threading.Thread(target=run, name=f"job: {name}", daemon=True).start()
        return job
