from django.urls import path

from .views import search_view


app_name = "search_ui"
urlpatterns = [path("", search_view, name="search")]
