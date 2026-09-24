import json

from django.core.management.base import BaseCommand

from apps.governance.audit import audit_health


class Command(BaseCommand):
    help = "Print a lightweight governance-audit head health summary."

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(audit_health(), sort_keys=True))
