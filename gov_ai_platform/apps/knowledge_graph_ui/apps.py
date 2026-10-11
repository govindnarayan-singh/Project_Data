from django.apps import AppConfig


class KnowledgeGraphUiConfig(AppConfig):
    name = "apps.knowledge_graph_ui"

    def ready(self):
        from .governance import register_reference_resolution_policy

        register_reference_resolution_policy()
