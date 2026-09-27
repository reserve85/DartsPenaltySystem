from django.urls import path

from app.players import views

app_name = "players"

urlpatterns = [
    # The players list was merged into the combined Teams & Players page.
    path("", views.PlayerListRedirectView.as_view(), name="player_list"),
    path("create/", views.PlayerCreateView.as_view(), name="player_create"),
    path("<int:pk>/", views.PlayerDetailView.as_view(), name="player_detail"),
    path("<int:pk>/edit/", views.PlayerUpdateView.as_view(), name="player_update"),
    path("<int:pk>/deactivate/", views.PlayerDeactivateView.as_view(), name="player_deactivate"),
]
