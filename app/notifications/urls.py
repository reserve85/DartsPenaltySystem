from django.urls import path

from app.notifications import views

app_name = "notifications_manage"

urlpatterns = [
    path("delete/<int:pk>/", views.NotificationDeleteView.as_view(), name="delete_one"),
    path("delete-read/", views.NotificationDeleteReadView.as_view(), name="delete_read"),
]
