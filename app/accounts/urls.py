"""Account URL routes.

Login/logout/signup/password reset & change/email verification are served by
django-allauth (mounted in ``config/urls.py`` before this module). Only the
application-specific pages live here.
"""

from django.urls import path

from app.accounts import views

app_name = "accounts"

urlpatterns = [
    path("settings/", views.SettingsView.as_view(), name="settings"),
    path("settings/theme/", views.ThemeUpdateView.as_view(), name="settings_theme"),
    path("users/", views.UserListView.as_view(), name="user_list"),
    path("users/create/", views.UserCreateView.as_view(), name="user_create"),
    path("users/pending/", views.PendingUserListView.as_view(), name="approval_list"),
    path("users/<int:pk>/approval/", views.UserApprovalView.as_view(), name="approval"),
    path("users/<int:pk>/update/", views.UserUpdateView.as_view(), name="user_update"),
    path("users/<int:pk>/deactivate/", views.UserDeactivateView.as_view(), name="user_deactivate"),
]
