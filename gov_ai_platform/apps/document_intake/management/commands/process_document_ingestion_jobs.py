from django.core.management.base import BaseCommand, CommandError

from apps.document_intake.ingestion import claim_ingestion_job, process_ingestion_job


class Command(BaseCommand):
    help = "Claim and process one durable Stage 3 document-ingestion job."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one available job.")
        parser.add_argument("--job", help="Target one job UUID.")

    def handle(self, *args, **options):
        if not options["once"] and not options["job"]:
            raise CommandError("Use --once or --job; this command never starts an implicit loop.")
        job = claim_ingestion_job(job_id=options["job"])
        if job is None:
            self.stdout.write("No pending ingestion job was claimable.")
            return
        result = process_ingestion_job(job)
        self.stdout.write(f"{result.pk} {result.state} attempts={result.attempt_count}")
