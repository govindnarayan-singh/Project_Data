import json

from django.core.management.base import BaseCommand

from apps.knowledge_graph_ui.workbench_sync import sync_unresolved_reference_workbench


class Command(BaseCommand):
    help = "Idempotently synchronize persistent unresolved-reference work items from authoritative KG candidates."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Calculate inventory and grouping without writes.")

    def handle(self, *args, **options):
        result = sync_unresolved_reference_workbench(dry_run=options["dry_run"])
        self.stdout.write(json.dumps(result.as_dict(), indent=2, sort_keys=True))
