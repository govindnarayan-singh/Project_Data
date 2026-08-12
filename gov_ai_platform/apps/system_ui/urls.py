from django.urls import path

from .views import corpus, evaluation

app_name = "system_ui"
urlpatterns = [path("corpus/", corpus, name="corpus"), path("evaluation/", evaluation, name="evaluation")]
