from django.shortcuts import render

from knowledge_engine.portal import PortalService


def get_portal_service():
    return PortalService()


def corpus(request):
    return render(request, "system/corpus.html", {
        **get_portal_service().corpus_dashboard(), "active_section": "corpus", "context_mode": "system",
    })


def evaluation(request):
    return render(request, "system/evaluation.html", {
        **get_portal_service().evaluation_dashboard(), "active_section": "evaluation", "context_mode": "system",
    })
