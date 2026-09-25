from django.urls import path

from app.penalties import views

app_name = "penalties"

urlpatterns = [
    path("penalties/catalog/", views.CatalogListView.as_view(), name="catalog_list"),
    path("penalties/catalog/create/", views.CatalogCreateView.as_view(), name="catalog_create"),
    path(
        "penalties/catalog/<int:pk>/edit/", views.CatalogUpdateView.as_view(), name="catalog_update"
    ),
    path(
        "penalties/catalog/<int:pk>/toggle-active/",
        views.CatalogToggleActiveView.as_view(),
        name="catalog_toggle_active",
    ),
    path(
        "penalties/catalog/<int:pk>/team-amounts/",
        views.CatalogTeamAmountsView.as_view(),
        name="catalog_team_amounts",
    ),
    path(
        "matchdays/<int:matchday_pk>/penalties/add/",
        views.PenaltyCreateView.as_view(),
        name="penalty_create",
    ),
    path("penalties/<int:pk>/edit/", views.PenaltyUpdateView.as_view(), name="penalty_update"),
    path("penalties/<int:pk>/delete/", views.PenaltyDeleteView.as_view(), name="penalty_delete"),
    path("payments/add/", views.PaymentCreateView.as_view(), name="payment_create"),
    path("payments/<int:pk>/delete/", views.PaymentDeleteView.as_view(), name="payment_delete"),
]
