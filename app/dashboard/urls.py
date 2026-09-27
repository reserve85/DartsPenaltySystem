from django.urls import path

from app.dashboard import views

app_name = "dashboard"

urlpatterns = [
    # The financial overview IS the start page — the old Dashboard/Übersicht
    # page only repeated the team list and was removed.
    path("", views.FinancialOverviewView.as_view(), name="index"),
    path("financial/", views.FinancialOverviewView.as_view(), name="financial_overview"),
]
