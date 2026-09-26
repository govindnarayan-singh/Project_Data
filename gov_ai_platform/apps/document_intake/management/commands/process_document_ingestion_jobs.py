from __future__ import annotations

import threading
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connections

from apps.document_intake.ingestion import (
    claim_ingestion_job,
    heartbeat_worker,
    process_ingestion_job,
    remove_worker_heartbeat,
    worker_identifier,
)
from knowledge_engine.graph.pipeline import KnowledgeGraphPipeline
from knowledge_engine.pipelines.batch_ingest import BatchIngestor


class LeaseHeartbeat:
    def __init__(self, worker_id: str, job_id):
        self.worker_id = worker_id
        self.job_id = job_id
        self.interval = max(5, min(settings.DOCUMENT_INGESTION_HEARTBEAT_SECONDS, 60))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="document-ingestion-heartbeat", daemon=True)

    def _run(self):
        close_old_connections()
        try:
            while not self._stop.wait(self.interval):
                heartbeat_worker(worker_id=self.worker_id, job_id=self.job_id)
        finally:
            connections.close_all()

    def __enter__(self):
        heartbeat_worker(worker_id=self.worker_id, job_id=self.job_id)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._stop.set()
        self._thread.join(timeout=self.interval + 1)
        heartbeat_worker(worker_id=self.worker_id, job_id=None)


class Command(BaseCommand):
    help = "Run the durable PostgreSQL-backed Stage 3 document-ingestion worker."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--once", action="store_true", help="Process at most one available job.")
        mode.add_argument("--loop", action="store_true", help="Poll continuously for eligible jobs.")
        parser.add_argument("--job-id", dest="job_id", help="Process one specific eligible job UUID.")
        parser.add_argument("--job", dest="legacy_job_id", help="Deprecated alias for --job-id.")
        parser.add_argument("--poll-seconds", type=float, default=5.0, help="Loop polling interval (0.25 to 60 seconds).")
        parser.add_argument("--max-jobs", type=int, help="Stop after this many claimed jobs.")

    def handle(self, *args, **options):
        job_id = options["job_id"] or options["legacy_job_id"]
        if options["job_id"] and options["legacy_job_id"]:
            raise CommandError("Use only one of --job-id or --job.")
        if job_id and options["loop"]:
            raise CommandError("--job-id is a bounded single-job mode and cannot be combined with --loop.")
        if not options["once"] and not options["loop"] and not job_id:
            raise CommandError("Use --once, --loop, or --job-id UUID.")
        poll_seconds = options["poll_seconds"]
        if not 0.25 <= poll_seconds <= 60:
            raise CommandError("--poll-seconds must be between 0.25 and 60.")
        max_jobs = options["max_jobs"]
        if max_jobs is not None and max_jobs < 1:
            raise CommandError("--max-jobs must be at least 1.")

        worker_id = worker_identifier()
        ingestor = BatchIngestor()
        graph_pipeline = KnowledgeGraphPipeline()
        processed = 0
        heartbeat_worker(worker_id=worker_id, job_id=None)
        try:
            while True:
                job = claim_ingestion_job(job_id=job_id, worker_id=worker_id)
                if job is None:
                    if options["loop"] and (max_jobs is None or processed < max_jobs):
                        heartbeat_worker(worker_id=worker_id, job_id=None)
                        time.sleep(poll_seconds)
                        continue
                    self.stdout.write("No eligible ingestion job was claimable.")
                    return
                self.stdout.write(f"Claimed {job.pk} attempt={job.attempt_count}/{job.max_attempts}")
                with LeaseHeartbeat(worker_id, job.pk):
                    result = process_ingestion_job(job, ingestor=ingestor, graph_pipeline=graph_pipeline)
                processed += 1
                self.stdout.write(f"{result.pk} {result.state} attempts={result.attempt_count}")
                if job_id or options["once"] or (max_jobs is not None and processed >= max_jobs):
                    return
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Worker stopped after the current safe boundary."))
        finally:
            remove_worker_heartbeat(worker_id=worker_id)
