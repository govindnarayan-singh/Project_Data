from django.apps import AppConfig


class DocumentIntakeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.document_intake"

    def ready(self):
        from .governance import register_document_admission_policy

        register_document_admission_policy()
