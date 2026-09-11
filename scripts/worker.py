#!/usr/bin/env python
"""CitePrism durable audit worker.

Run one or more of these alongside the API. Each polls the workspace queue,
leases a job, renews the lease while the audit runs, and releases or fails it on
completion. Stopping a worker with SIGTERM finishes cleanly; killing it loses at
most one lease interval before another worker picks the job up.

    python scripts/worker.py --concurrency 2
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CitePrism durable audit worker")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="worker loops in this process (default: 1)")
    parser.add_argument("--worker-id", default=None, help="override the reported worker identity")
    parser.add_argument("--once", action="store_true",
                        help="execute at most one job, then exit (useful in tests)")
    parser.add_argument("--kinds", default="audit", help="comma-separated job kinds to consume")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    arguments = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(arguments.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("citeprism.worker")

    from config.settings import Config
    from src.workspace.executor import AuditExecutor
    from src.workspace.factory import (
        build_repair_service,
        build_runtime,
        ensure_workspace_schema,
    )
    from src.workspace.queue import DurableQueueWorker, QueueSettings

    Config.validate_phase4()
    applied = ensure_workspace_schema(Config)
    if applied:
        logger.info("Applied workspace migrations: %s", ", ".join(applied))

    runtime = build_runtime(Config)
    settings = QueueSettings.from_config(Config)
    executor = AuditExecutor(
        runtime["store"],
        runtime["pipeline"],
        config=Config,
        pricing=runtime["pricing"],
        queue_settings=settings,
        repair_service=build_repair_service(runtime),
    )
    executor.object_store = runtime["object_store"]
    kinds = tuple(item.strip() for item in arguments.kinds.split(",") if item.strip())

    logger.info(
        "Worker starting. backend=%s objects=%s concurrency=%d lease=%ds",
        Config.WORKSPACE_BACKEND,
        Config.WORKSPACE_OBJECT_BACKEND,
        arguments.concurrency,
        settings.lease_seconds,
    )

    if arguments.once:
        worker = DurableQueueWorker(
            runtime["store"], executor, settings=settings,
            worker_id=arguments.worker_id, kinds=kinds,
        )
        return 0 if worker.run_once() else 1

    if arguments.concurrency > 1:
        from src.workspace.queue import EmbeddedQueueWorker

        pool = EmbeddedQueueWorker(
            runtime["store"], executor, settings=settings, concurrency=arguments.concurrency
        )
        pool.start()
        try:
            import threading

            threading.Event().wait()
        except KeyboardInterrupt:
            logger.info("Stopping workers…")
            pool.shutdown(wait=True)
        return 0

    worker = DurableQueueWorker(
        runtime["store"], executor, settings=settings,
        worker_id=arguments.worker_id, kinds=kinds,
    )
    worker.install_signal_handlers()
    worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
