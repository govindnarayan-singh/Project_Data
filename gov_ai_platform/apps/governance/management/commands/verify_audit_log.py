from django.core.management.base import BaseCommand, CommandError

from apps.governance.audit import verify_audit_chain


class Command(BaseCommand):
    help = "Verify sequence, previous-hash, event-hash, and chain-head continuity."

    def handle(self, *args, **options):
        result = verify_audit_chain()
        if not result.valid:
            self.stdout.write("FAIL")
            self.stdout.write(f"first broken sequence: {result.first_broken_sequence}")
            self.stdout.write(f"reason: {result.reason}")
            raise CommandError("Governance audit verification failed.")
        self.stdout.write("PASS")
        self.stdout.write(f"events checked: {result.events_checked}")
        self.stdout.write(f"first sequence: {result.first_sequence}")
        self.stdout.write(f"last sequence: {result.last_sequence}")
        self.stdout.write(f"head hash: {result.head_hash}")
