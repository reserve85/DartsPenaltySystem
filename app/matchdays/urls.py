from django.urls import path

from app.matchdays import views

app_name = "matchdays"

urlpatterns = [
    path("", views.MatchdayListView.as_view(), name="matchday_list"),
    path("create/", views.MatchdayCreateView.as_view(), name="matchday_create"),
    path("<int:pk>/", views.MatchdayDetailView.as_view(), name="matchday_detail"),
    path("<int:pk>/edit/", views.MatchdayUpdateView.as_view(), name="matchday_update"),
    path("<int:pk>/delete/", views.MatchdayDeleteView.as_view(), name="matchday_delete"),
]
