"""Worker loop: claim a job (lease), keep the lease alive with a heartbeat, run the engine, release.

Run it as its own process (python -m relay.worker). Kill it at any time: the lease expires and another
worker (or the restarted one) resumes the run from its last persisted step.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid

from .catalog import Catalog
from .config import Settings
from .db import Store
from .engine import Engine, LeaseLost
from .providers import build_provider
from .jobqueue import JobQueue

log = logging.getLogger("relay.worker")


class Worker:
    def __init__(self, store: Store, settings: Settings, engine: Engine, worker_id: str | None = None):
        self.store, self.settings, self.engine = store, settings, engine
        self.worker_id = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.queue = JobQueue(store, settings.lease_seconds)

    def run_once(self) -> bool:
        """Process at most one job. Returns False when the queue was empty."""
        job = self.queue.claim(self.worker_id)
        if job is None:
            return False
        run_id = job["run_id"]
        if job["deliveries"] > self.settings.max_job_deliveries:
            self.engine.fail_internal(run_id, RuntimeError(
                f"run was delivered {job['deliveries']} times without finishing (crash loop?)"))
            return True
        log.info("%s picked up %s (delivery %d)", self.worker_id, run_id, job["deliveries"])
        stop = threading.Event()
        beat = threading.Thread(target=self._heartbeat, args=(run_id, stop), daemon=True)
        beat.start()
        try:
            self.engine.process_run(run_id, self.worker_id)
        except LeaseLost:
            log.warning("lost lease on %s; another worker owns it now", run_id)
        except Exception as exc:  # an engine bug must not wedge the queue
            log.exception("unexpected error in %s", run_id)
            self.engine.fail_internal(run_id, exc)
        finally:
            stop.set()
            beat.join(timeout=2)
        self.queue.release(run_id, self.worker_id)
        return True

    def _heartbeat(self, run_id: str, stop: threading.Event) -> None:
        while not stop.wait(self.settings.lease_seconds / 3):
            try:
                self.queue.renew(run_id, self.worker_id)
            except Exception:  # transient DB lock: next beat will retry
                log.warning("heartbeat failed for %s", run_id)

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        log.info("%s started (db=%s, world=%s)", self.worker_id, self.settings.db_path, self.settings.world_url)
        while not stop.is_set():
            if not self.run_once():
                stop.wait(self.settings.poll_interval)


def build_worker(settings: Settings) -> Worker:
    store = Store(settings.db_path)
    catalog = Catalog.load(settings.data_dir / "node_catalog.json")
    engine = Engine(store, settings, catalog, build_provider(settings.provider))
    return Worker(store, settings, engine)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        build_worker(Settings.from_env()).run_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
