from django.urls import path

from app.dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="index"),
    path("financial/", views.FinancialOverviewView.as_view(), name="financial_overview"),
]
