from django.urls import path

from app.teams import views

app_name = "teams"

urlpatterns = [
    path("", views.TeamListView.as_view(), name="team_list"),
    path("create/", views.TeamCreateView.as_view(), name="team_create"),
    # Old team page -> financial overview (which now shows the roster/balances).
    path("<int:pk>/", views.TeamDetailRedirectView.as_view(), name="team_detail"),
    path("<int:pk>/edit/", views.TeamUpdateView.as_view(), name="team_update"),
    path("<int:pk>/delete/", views.TeamDeleteView.as_view(), name="team_delete"),
]
