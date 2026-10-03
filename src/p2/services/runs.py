"""Runs in the background. A Run (live refresh or final analysis) takes one to two minutes, so clicking Run only puts it in a queue and
returns at once; a few worker threads do the work.

Rules, kept deliberately small:
- At most `max_concurrent` Runs at the same time (default 4), the rest wait in order, so a crowd cannot flood BigQuery.
- At most one Run per experiment at a time: asking again while one is queued or running returns that Run instead of starting a second
  (two Runs rebuilding the same tables would mix their numbers).
- A temporary BigQuery problem (rate limit, busy, brief outage) is retried a few times with growing pauses; any other failure is final.
- Everything is held in this server's memory. If the server restarts, queued Runs are gone (people simply press Run again).
"""
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from p2.services import errors

log = logging.getLogger(__name__)
MAX_WAITING = 50


@dataclass
class Ticket:
    experiment_id: str
    kind: str                     # "monitor" or "final"
    actor: object
    state: str = "queued"         # queued, running, succeeded or failed
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    attempt: int = 0
    error: str | None = None
    result: object = None
    duplicate: bool = False       # True on the copy handed back when a Run for this experiment was already waiting or running

    def view(self, position: int | None = None) -> dict:
        now = time.time()
        return {"experiment_id": self.experiment_id, "kind": self.kind, "state": self.state, "position": position, "attempt": self.attempt,
                "error": self.error, "waited": round((self.started_at or now) - self.queued_at, 1),
                "running_for": None if self.started_at is None else round((self.finished_at or now) - self.started_at, 1),
                "result": self.result, "duplicate": self.duplicate}


class RunManager:
    def __init__(self, work, max_concurrent: int = 4, retry_pauses: tuple[float, ...] = (5.0, 15.0, 45.0), inline: bool = False,
                 sleep=time.sleep, keep_finished: int = 100):
        """`work(actor, experiment_id, final)` does one Run and returns its result or raises. With `inline` the Run happens in the caller's
        thread before submit() returns (used by tests and scripts that want the old, simple behaviour)."""
        self.work = work
        self.max_concurrent = max_concurrent
        self.retry_pauses = retry_pauses
        self.inline = inline
        self.sleep = sleep
        self.keep_finished = keep_finished
        self._cv = threading.Condition()
        self._waiting: deque[Ticket] = deque()
        self._running: dict[str, Ticket] = {}
        self._finished: dict[str, Ticket] = {}            # the latest finished Run of each experiment
        self._workers: list[threading.Thread] = []
        self._stop = False
        self.peak_running = 0

    # ---- asking ---------------------------------------------------------------------------------
    def submit(self, actor, experiment_id: str, final: bool) -> Ticket:
        """Queue a Run (or run it now when inline). Returns the Ticket; if this experiment already has a Run waiting or running, returns
        that Run marked `duplicate`."""
        ticket = Ticket(experiment_id, "final" if final else "monitor", actor)
        if self.inline:
            self._execute(ticket)
            if ticket.state == "failed":                  # keep a failure visible, like a queued Run's, until it is dismissed
                with self._cv:
                    self._finished[experiment_id] = ticket
            return ticket
        with self._cv:
            existing = self._running.get(experiment_id) or next((t for t in self._waiting if t.experiment_id == experiment_id), None)
            if existing is not None:
                twin = Ticket(**{**existing.__dict__, "duplicate": True})
                return twin
            if len(self._waiting) >= MAX_WAITING:
                raise errors.InvalidInput(f"{len(self._waiting)} Runs are already waiting; try again in a few minutes")
            self._finished.pop(experiment_id, None)
            self._waiting.append(ticket)
            self._ensure_workers()
            self._cv.notify()
        return ticket

    def status(self, experiment_id: str) -> dict | None:
        """The queued, running or most recently finished Run of this experiment, or None."""
        with self._cv:
            if experiment_id in self._running:
                return self._running[experiment_id].view()
            for i, t in enumerate(self._waiting):
                if t.experiment_id == experiment_id:
                    return t.view(position=i + 1)
            done = self._finished.get(experiment_id)
            return done.view() if done else None

    def acknowledge(self, experiment_id: str) -> None:
        """Forget a finished Run once its result has been shown."""
        with self._cv:
            self._finished.pop(experiment_id, None)

    def active(self) -> list[dict]:
        """Every queued or running Run, running first."""
        with self._cv:
            return [t.view() for t in self._running.values()] + [t.view(position=i + 1) for i, t in enumerate(self._waiting)]

    def wait_all(self, timeout: float = 30.0) -> bool:
        """Block until nothing is queued or running (for tests and scripts). Returns False on timeout."""
        deadline = time.time() + timeout
        with self._cv:
            while self._waiting or self._running:
                left = deadline - time.time()
                if left <= 0:
                    return False
                self._cv.wait(min(left, 0.1))
        return True

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    # ---- the workers ----------------------------------------------------------------------------------
    def _ensure_workers(self) -> None:
        self._workers = [w for w in self._workers if w.is_alive()]
        while len(self._workers) < self.max_concurrent:
            w = threading.Thread(target=self._loop, name=f"run-worker-{len(self._workers)}", daemon=True)
            self._workers.append(w)
            w.start()

    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._waiting and not self._stop:
                    self._cv.wait()
                if self._stop and not self._waiting:
                    return
                ticket = self._waiting.popleft()
                self._running[ticket.experiment_id] = ticket
                self.peak_running = max(self.peak_running, len(self._running))
            self._execute(ticket)
            with self._cv:
                self._running.pop(ticket.experiment_id, None)
                self._finished[ticket.experiment_id] = ticket
                while len(self._finished) > self.keep_finished:
                    self._finished.pop(next(iter(self._finished)))
                self._cv.notify_all()

    def _execute(self, ticket: Ticket) -> None:
        ticket.state, ticket.started_at = "running", time.time()
        pauses = list(self.retry_pauses)
        while True:
            ticket.attempt += 1
            try:
                ticket.result = self.work(ticket.actor, ticket.experiment_id, ticket.kind == "final")
                ticket.state, ticket.error = "succeeded", None
                break
            except errors.TransientError as e:
                if not pauses:
                    ticket.state, ticket.error = "failed", f"BigQuery stayed busy after {ticket.attempt} tries: {e}"
                    break
                log.warning("run of %s hit a temporary problem (try %d): %s", ticket.experiment_id, ticket.attempt, e)
                self.sleep(pauses.pop(0))
            except Exception as e:                       # a real failure: show it, do not retry
                ticket.state, ticket.error = "failed", str(e)
                break
        ticket.finished_at = time.time()
