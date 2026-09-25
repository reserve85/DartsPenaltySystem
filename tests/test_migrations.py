"""makemigrations --check must pass — no missing migrations."""

import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_no_missing_migrations():
    call_command("makemigrations", check=True, dry_run=True)
